"""CoinDCX 15-minute 30-day High/Low breakout + 10x volume Telegram screener."""

from __future__ import annotations
import json, logging, os, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
COINDCX_CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
CANDLE_LIMIT = 1000
DAILY_LIMIT = 40
MONTH_DAYS = 30
VOLUME_LOOKBACK = 20
TRIGGER_VOLUME_MULTIPLE = 10.0
REQUEST_TIMEOUT = 20
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.15"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

def as_float(v: Any) -> float:
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def normalize_timestamp(v: Any) -> int:
    ts = int(as_float(v)); return ts * 1000 if 1_000_000_000 <= ts < 10_000_000_000 else ts

def normalize_candle(raw: dict[str, Any]) -> Candle | None:
    c = Candle(normalize_timestamp(raw.get("time") or raw.get("timestamp") or raw.get("t")), as_float(raw.get("open") or raw.get("o")), as_float(raw.get("high") or raw.get("h")), as_float(raw.get("low") or raw.get("l")), as_float(raw.get("close") or raw.get("c")), as_float(raw.get("volume") or raw.get("v")))
    return c if c.timestamp > 0 and c.high > 0 and c.low > 0 and c.close > 0 else None

def parse_candles(payload: Any) -> list[Candle]:
    raw = payload.get("data") or payload.get("candles") or [] if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    out = [normalize_candle(x) for x in raw if isinstance(x, dict)]
    return sorted((x for x in out if x), key=lambda x: x.timestamp)

def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists(): return {"sent": []}
    try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {"sent": []}

def save_state(s: dict[str, Any]) -> None: STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")

def is_usdt(m: dict[str, Any]) -> bool:
    return str(m.get("ecode", "")).upper() == "B" and str(m.get("status", "")).lower() in {"active", "online"} and str(m.get("target_currency_short_name", "")).upper() == "USDT"

def get_markets(s: requests.Session) -> list[tuple[str, str]]:
    r = s.get(COINDCX_MARKETS_URL, timeout=REQUEST_TIMEOUT); r.raise_for_status()
    out = []
    for m in r.json():
        if not isinstance(m, dict) or not is_usdt(m): continue
        pair = m.get("pair") or m.get("coindcx_name") or m.get("symbol")
        if not pair: continue
        pair = str(pair).upper(); name = str(m.get("coindcx_name") or m.get("symbol") or pair).upper()
        if name.startswith("B-"): name = name[2:]
        out.append((pair, name.replace("_", "")))
    return sorted(set(out))

def get_candles(s: requests.Session, pair: str, interval: str, limit: int) -> list[Candle]:
    r = s.get(COINDCX_CANDLES_URL, params={"pair": pair, "interval": interval, "limit": limit}, timeout=REQUEST_TIMEOUT); r.raise_for_status(); return parse_candles(r.json())

def closed_15m(c: list[Candle]) -> list[Candle]:
    now = int(time.time()*1000); return [x for x in c if x.timestamp + INTERVAL_MS <= now]

def month_levels(s: requests.Session, pair: str) -> tuple[float,float] | None:
    d = get_candles(s, pair, "1d", DAILY_LIMIT)
    if len(d) < MONTH_DAYS + 1: return None
    d = d[:-1][-MONTH_DAYS:]
    return max(x.high for x in d), min(x.low for x in d)

def send_telegram(s: requests.Session, text: str) -> bool:
    token, chat = os.getenv("BOT_TOKEN"), os.getenv("CHAT_ID")
    if not token or not chat: LOGGER.warning("BOT_TOKEN/CHAT_ID missing"); return False
    try:
        r = s.post(TELEGRAM_URL.format(token=token), json={"chat_id":chat,"text":text,"disable_web_page_preview":True}, timeout=REQUEST_TIMEOUT); r.raise_for_status(); return True
    except requests.RequestException as e: LOGGER.warning("Telegram send failed: %s", e); return False

def alert_text(name: str, side: str, level: float, c: Candle, vx: float) -> str:
    title, label = (("🟢 COINDCX 1-MONTH HIGH BREAK", "1M High") if side == "UP" else ("🔴 COINDCX 1-MONTH LOW BREAK", "1M Low"))
    when = datetime.fromtimestamp(c.timestamp/1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{title}\n\nCoin: {name}\n{label}: {level:g}\nBreak Price: {c.close:g}\n15M Volume: {c.volume:,.2f}\nVolume: {vx:.1f}x previous 20-candle average\nCandle: {when}"

def scan_pair(s: requests.Session, pair: str, name: str, state: dict[str,Any]) -> bool:
    c = closed_15m(get_candles(s,pair,INTERVAL,CANDLE_LIMIT))
    if len(c) < VOLUME_LOOKBACK + 2: return False
    trigger = c[-1]; avg = sum(x.volume for x in c[-1-VOLUME_LOOKBACK:-1]) / VOLUME_LOOKBACK
    if avg <= 0 or trigger.volume < avg * TRIGGER_VOLUME_MULTIPLE: return False
    levels = month_levels(s,pair)
    if levels is None: return False
    hi, lo = levels
    if trigger.high > hi:
        key=f"{pair}:HIGH:{trigger.timestamp}"
        if key in state["sent"]: return False
        if send_telegram(s,alert_text(name,"UP",hi,trigger,trigger.volume/avg)): state["sent"].append(key); return True
    if trigger.low < lo:
        key=f"{pair}:LOW:{trigger.timestamp}"
        if key in state["sent"]: return False
        if send_telegram(s,alert_text(name,"DOWN",lo,trigger,trigger.volume/avg)): state["sent"].append(key); return True
    return False

def main() -> None:
    LOGGER.info("RAKESH COINDCX 1-MONTH HIGH/LOW VOLUME SCREENER | 15m | >=10x volume")
    state=load_state(); s=requests.Session(); s.headers.update({"User-Agent":"Rakesh-CoinDCX-1M-HL-15m-10x/1.0"})
    markets=get_markets(s); LOGGER.info("Active USDT pairs: %d",len(markets)); alerts=0
    for i,(pair,name) in enumerate(markets,1):
        try:
            if scan_pair(s,pair,name,state): alerts+=1; save_state(state); LOGGER.info("ALERT SENT: %s",name)
        except requests.RequestException as e: LOGGER.warning("%s API error: %s",name,e)
        except Exception as e: LOGGER.warning("%s scan error: %s",name,e)
        if i%50==0: LOGGER.info("Progress: %d/%d",i,len(markets))
        time.sleep(SCAN_SLEEP_SECONDS)
    state["sent"]=state.get("sent",[])[-5000:]; save_state(state); LOGGER.info("Scan complete | pairs=%d | alerts=%d",len(markets),alerts)

if __name__ == "__main__": main()
