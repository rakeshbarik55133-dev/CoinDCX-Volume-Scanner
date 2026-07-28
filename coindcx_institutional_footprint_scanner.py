"""Scan CoinDCX spot markets for institutional-footprint candles.

The scanner uses only CoinDCX's public market metadata and OHLCV candle APIs.
Configure ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` before running it.
It intentionally has no dependency beyond ``requests`` so it remains suitable
for Termux and other small Python installations.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests

MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

TIMEFRAME = os.getenv("COINDCX_FOOTPRINT_TIMEFRAME", "15m")
TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
                "1d": 86_400_000}.get(TIMEFRAME)
if TIMEFRAME_MS is None:
    raise ValueError("Unsupported COINDCX_FOOTPRINT_TIMEFRAME")

LOOKBACK = int(os.getenv("COINDCX_FOOTPRINT_LOOKBACK", "20"))
CANDLE_LIMIT = max(LOOKBACK + 5, 30)
VOLUME_MULTIPLIER = float(os.getenv("COINDCX_VOLUME_MULTIPLIER", "3.0"))
RANGE_MULTIPLIER = float(os.getenv("COINDCX_RANGE_MULTIPLIER", "2.0"))
MIN_BODY_FRACTION = float(os.getenv("COINDCX_MIN_BODY_FRACTION", "0.60"))
MIN_CLOSE_LOCATION = float(os.getenv("COINDCX_MIN_CLOSE_LOCATION", "0.80"))
SCAN_INTERVAL = float(os.getenv("COINDCX_SCAN_INTERVAL", "60"))
PAIR_DELAY = float(os.getenv("COINDCX_PAIR_DELAY", "0.10"))
REQUEST_TIMEOUT = float(os.getenv("COINDCX_REQUEST_TIMEOUT", "20"))
MAX_RETRIES = int(os.getenv("COINDCX_MAX_RETRIES", "3"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.getenv("COINDCX_FOOTPRINT_STATE", ".coindcx_footprint_state.json"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("coindcx_footprint")
STOP = False


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def candle_range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class Footprint:
    market_pair: str
    symbol: str
    side: Literal["BULLISH", "BEARISH"]
    candle: Candle
    volume_ratio: float
    range_ratio: float
    broken_level: float

    @property
    def key(self) -> str:
        return f"{self.market_pair}:{self.side}:{self.candle.timestamp}"


def request_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    """Request JSON with a small bounded retry suitable for mobile networks."""
    error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"CoinDCX request failed after {MAX_RETRIES} attempts") from error


def active_usdt_pairs(session: requests.Session) -> dict[str, str]:
    """Return every active CoinDCX USDT spot candle pair and display name."""
    markets = request_json(session, "GET", MARKETS_URL)
    if not isinstance(markets, list):
        raise RuntimeError("Unexpected CoinDCX markets response")
    pairs: dict[str, str] = {}
    for market in markets:
        if not isinstance(market, dict):
            continue
        status = str(market.get("status", "")).lower()
        quote = str(market.get("target_currency_short_name", "")).upper()
        pair = str(market.get("pair", "")).strip().upper()
        if status in {"active", "online"} and quote == "USDT" and pair:
            name = str(market.get("coindcx_name") or market.get("symbol") or pair)
            pairs[pair] = name.upper().replace("_", "").removeprefix("B-")
    return dict(sorted(pairs.items()))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_candles(payload: Any) -> list[Candle]:
    """Parse CoinDCX OHLCV objects, accepting documented long or short keys."""
    rows = payload.get("data", payload.get("candles", [])) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    parsed: dict[int, Candle] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = int(_float(row.get("time", row.get("timestamp", row.get("t")))))
        if 1_000_000_000 <= timestamp < 10_000_000_000:
            timestamp *= 1000
        candle = Candle(timestamp, _float(row.get("open", row.get("o"))),
                        _float(row.get("high", row.get("h"))),
                        _float(row.get("low", row.get("l"))),
                        _float(row.get("close", row.get("c"))),
                        _float(row.get("volume", row.get("v"))))
        if (timestamp > 0 and candle.low > 0 and candle.low <= candle.open <= candle.high
                and candle.low <= candle.close <= candle.high and candle.volume >= 0):
            parsed[timestamp] = candle
    return sorted(parsed.values(), key=lambda item: item.timestamp)


def fetch_closed_candles(session: requests.Session, pair: str) -> list[Candle]:
    payload = request_json(session, "GET", CANDLES_URL,
                           params={"pair": pair, "interval": TIMEFRAME, "limit": CANDLE_LIMIT})
    now = int(time.time() * 1000)
    return [candle for candle in parse_candles(payload)
            if candle.timestamp + TIMEFRAME_MS <= now]


def detect_footprint(pair: str, symbol: str, candles: list[Candle]) -> Footprint | None:
    """Detect ultra-volume, wide-range, strong-close breakouts/breakdowns."""
    if len(candles) < LOOKBACK + 1:
        return None
    current = candles[-1]
    baseline = candles[-LOOKBACK - 1:-1]
    average_volume = sum(c.volume for c in baseline) / LOOKBACK
    average_range = sum(c.candle_range for c in baseline) / LOOKBACK
    if average_volume <= 0 or average_range <= 0 or current.candle_range <= 0:
        return None
    volume_ratio = current.volume / average_volume
    range_ratio = current.candle_range / average_range
    body_fraction = abs(current.close - current.open) / current.candle_range
    close_location = (current.close - current.low) / current.candle_range
    if (volume_ratio < VOLUME_MULTIPLIER or range_ratio < RANGE_MULTIPLIER
            or body_fraction < MIN_BODY_FRACTION):
        return None

    prior_high = max(c.high for c in baseline)
    prior_low = min(c.low for c in baseline)
    if current.close > prior_high and close_location >= MIN_CLOSE_LOCATION:
        return Footprint(pair, symbol, "BULLISH", current, volume_ratio, range_ratio, prior_high)
    if current.close < prior_low and close_location <= 1 - MIN_CLOSE_LOCATION:
        return Footprint(pair, symbol, "BEARISH", current, volume_ratio, range_ratio, prior_low)
    return None


def alert_text(found: Footprint) -> str:
    icon = "🟢" if found.side == "BULLISH" else "🔴"
    action = "Breakout" if found.side == "BULLISH" else "Breakdown"
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(found.candle.timestamp / 1000))
    return (f"{icon} CoinDCX Institutional Footprint — {found.side}\n"
            f"Pair: {found.symbol}\nTimeframe: {TIMEFRAME}\n"
            f"{action} close: {found.candle.close:.10g}\n"
            f"Broken level: {found.broken_level:.10g}\n"
            f"Volume: {found.volume_ratio:.2f}x {LOOKBACK}-candle average\n"
            f"Range: {found.range_ratio:.2f}x {LOOKBACK}-candle average\nCandle: {when}")


def send_telegram(session: requests.Session, text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    result = request_json(session, "POST", TELEGRAM_URL.format(token=BOT_TOKEN),
                          json={"chat_id": CHAT_ID, "text": text,
                                "disable_web_page_preview": True})
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the alert")


def load_state() -> dict[str, int]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {str(key): int(value) for key, value in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def save_state(state: dict[str, int]) -> None:
    cutoff = int(time.time() * 1000) - 30 * 86_400_000
    current = {key: value for key, value in state.items() if value >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
    temporary.replace(STATE_FILE)


def scan_once(session: requests.Session, sent: dict[str, int]) -> int:
    pairs = active_usdt_pairs(session)
    LOG.info("Scanning %d active CoinDCX USDT pairs", len(pairs))
    alerts = 0
    for pair, symbol in pairs.items():
        if STOP:
            break
        try:
            found = detect_footprint(pair, symbol, fetch_closed_candles(session, pair))
            if found and found.key not in sent:
                send_telegram(session, alert_text(found))
                sent[found.key] = found.candle.timestamp
                save_state(sent)
                alerts += 1
                LOG.info("Alerted %s %s", symbol, found.side)
        except Exception as exc:  # Keep one unavailable market from stopping the universe scan.
            LOG.warning("Skipping %s: %s", pair, exc)
        if PAIR_DELAY > 0:
            time.sleep(PAIR_DELAY)
    return alerts


def _stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before starting")
    sent = load_state()
    with requests.Session() as session:
        session.headers["User-Agent"] = "CoinDCX-Institutional-Footprint-Scanner/1.0"
        send_telegram(session, f"🚀 CoinDCX institutional footprint scanner started ({TIMEFRAME})")
        while not STOP:
            started = time.monotonic()
            try:
                scan_once(session, sent)
            except Exception as exc:
                LOG.error("Universe scan failed: %s", exc)
            wait = max(0.0, SCAN_INTERVAL - (time.monotonic() - started))
            deadline = time.monotonic() + wait
            while not STOP and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))


if __name__ == "__main__":
    main()
