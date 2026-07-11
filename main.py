"""CoinDCX USDT sideways-base 15-minute breakout Telegram scanner."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
COINDCX_CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

BASE_INTERVAL = "15m"
TRIGGER_INTERVAL = BASE_INTERVAL
BASE_INTERVAL_MS = 15 * 60 * 1000
PAIR_REFRESH_SECONDS = 30 * 60
CANDLE_LIMIT = 120
REQUEST_TIMEOUT = 20
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.15"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))
ALERT_PAIR_NAMES: dict[str, str] = {}
INVALID_CANDLE_PAIRS: set[str] = set()
LOGGED_INVALID_CANDLE_PAIRS: set[str] = set()

# Sideways-base detection. These are intentionally limited to base shape and
# base volume only; no indicators, ranking, confirmation candles, or late-entry
# filters are used.
BASE_LOOKBACK = 12
MIN_HISTORY = BASE_LOOKBACK
MAX_BASE_RANGE_PCT = 0.018
MAX_BASE_DRIFT_PCT = 0.008
MAX_BASE_VOLUME_VARIATION_RATIO = 2.5
TRIGGER_VOLUME_MULTIPLE = 3.0
SETUP_EXPIRY_SECONDS = int(os.getenv("SETUP_EXPIRY_SECONDS", str(6 * 60 * 60)))
RUN_FOREVER = os.getenv("RUN_FOREVER", "0") == "1"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class BaseSetup:
    pair: str
    base_end_timestamp: int
    base_high: float
    base_low: float
    reference_volume: float
    expires_at: int

    @property
    def setup_key(self) -> str:
        return f"{self.pair}:{self.base_end_timestamp}:{self.base_high:g}:{self.base_low:g}:{self.reference_volume:g}"


@dataclass(frozen=True)
class Signal:
    pair: str
    side: str
    candle: Candle
    setup: BaseSetup
    volume_ratio: float

    @property
    def alert_key(self) -> str:
        return f"{self.setup.setup_key}:{self.side}"

    @property
    def direction(self) -> str:
        return "BREAKOUT" if self.side == "BUY" else "BREAKDOWN"

    @property
    def break_level(self) -> float:
        return self.setup.base_high if self.side == "BUY" else self.setup.base_low


@dataclass(frozen=True)
class SignalEvaluation:
    signal: Signal | None
    rejection_reason: str | None = None


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_timestamp(value: Any) -> int:
    timestamp = int(as_float(value))
    if 1_000_000_000 <= timestamp < 10_000_000_000:
        return timestamp * 1000
    return timestamp


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"sent_alerts": set(), "setups": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Could not read alert state; starting with empty state")
        return {"sent_alerts": set(), "setups": {}}
    return {
        "sent_alerts": set(data.get("sent_alerts", [])),
        "setups": data.get("setups", {}),
    }


def save_state(sent_alerts: set[str], setups: dict[str, BaseSetup]) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {
                "sent_alerts": sorted(sent_alerts),
                "setups": {pair: asdict(setup) for pair, setup in sorted(setups.items())},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def is_usdt_market(market: dict[str, Any]) -> bool:
    quote_values = [
        market.get("target_currency_short_name"),
        market.get("quote_currency_short_name"),
        market.get("quote_currency"),
    ]
    if any(str(value).upper() == "USDT" for value in quote_values if value):
        return True
    pair_name = str(market.get("coindcx_name") or market.get("symbol") or market.get("pair") or "").upper()
    return pair_name.endswith("USDT") or pair_name.endswith("_USDT")


def coindcx_alert_pair_name(market: dict[str, Any], pair: str) -> str:
    name = str(market.get("coindcx_name") or market.get("symbol") or pair).upper()
    return re.sub(r"^[A-Z]-", "", name).replace("_", "")


def coindcx_candle_pair_name(market: dict[str, Any]) -> str | None:
    """Return the exact market metadata pair used by the public candles endpoint."""
    pair = market.get("pair")
    if pair:
        return str(pair).upper()
    for field in ("coindcx_name", "symbol"):
        value = market.get(field)
        if value:
            pair_text = str(value).upper()
            if pair_text:
                return pair_text
    return None


def alert_pair_name(pair: str) -> str:
    return ALERT_PAIR_NAMES.get(pair, re.sub(r"^[A-Z]-", "", pair.upper()).replace("_", ""))


def get_usdt_pairs(session: requests.Session) -> list[str]:
    response = session.get(COINDCX_MARKETS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    pairs: set[str] = set()
    for market in response.json():
        pair = market.get("pair")
        if not pair or not is_usdt_market(market):
            continue
        status = str(market.get("status", "active")).lower()
        if status in {"active", "online"}:
            pair_text = coindcx_candle_pair_name(market)
            if pair_text is None or pair_text in INVALID_CANDLE_PAIRS:
                continue
            pairs.add(pair_text)
            ALERT_PAIR_NAMES[pair_text] = coindcx_alert_pair_name(market, str(pair))
    return sorted(pairs)


def normalize_candle(raw: dict[str, Any] | list[Any]) -> Candle | None:
    if isinstance(raw, list):
        timestamp, open_price, high, low, close, volume = (raw + [0] * 6)[:6]
    else:
        timestamp = raw.get("time") or raw.get("timestamp") or raw.get("t")
        open_price = raw.get("open") or raw.get("o")
        high = raw.get("high") or raw.get("h")
        low = raw.get("low") or raw.get("l")
        close = raw.get("close") or raw.get("c")
        volume = raw.get("volume") or raw.get("v")
    candle = Candle(normalize_timestamp(timestamp), as_float(open_price), as_float(high), as_float(low), as_float(close), as_float(volume))
    if candle.timestamp <= 0 or candle.high <= 0 or candle.low <= 0 or candle.close <= 0:
        return None
    return candle


def parse_candles(payload: Any) -> list[Candle]:
    if isinstance(payload, dict):
        raw_candles = payload.get("data") or payload.get("candles") or []
    elif isinstance(payload, list):
        raw_candles = payload
    else:
        raw_candles = []
    candles = [normalize_candle(item) for item in raw_candles if isinstance(item, (dict, list))]
    return sorted((candle for candle in candles if candle), key=lambda candle: candle.timestamp)


def is_http_422(exc: requests.RequestException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 422


def remember_invalid_candle_pair(pair: str, interval: str) -> None:
    INVALID_CANDLE_PAIRS.add(pair)
    if pair in LOGGED_INVALID_CANDLE_PAIRS:
        return
    LOGGED_INVALID_CANDLE_PAIRS.add(pair)
    LOGGER.warning("%s candle endpoint returned HTTP 422 for %s; excluding for the rest of this run", pair, interval)


def get_candles(session: requests.Session, pair: str, interval: str = BASE_INTERVAL, log_response: bool = False) -> list[Candle]:
    response = session.get(
        COINDCX_CANDLES_URL,
        params={"pair": pair, "interval": interval, "limit": CANDLE_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    if log_response:
        LOGGER.info("%s %s candle response status=%s body=%s", pair, interval, getattr(response, "status_code", "unknown"), getattr(response, "text", ""))
    response.raise_for_status()
    return parse_candles(response.json())


def get_closed_candles(candles: list[Candle], interval_ms: int = BASE_INTERVAL_MS) -> list[Candle]:
    now_ms = int(time.time() * 1000)
    return [candle for candle in candles if candle.timestamp + interval_ms <= now_ms]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def find_latest_sideways_base(pair: str, candles: list[Candle]) -> BaseSetup | None:
    closed = get_closed_candles(candles, BASE_INTERVAL_MS)
    if len(closed) < MIN_HISTORY:
        return None
    base = closed[-BASE_LOOKBACK:]
    base_high = max(candle.high for candle in base)
    base_low = min(candle.low for candle in base)
    base_mid = (base_high + base_low) / 2
    if base_mid <= 0 or (base_high - base_low) / base_mid > MAX_BASE_RANGE_PCT:
        return None
    if abs(base[-1].close - base[0].open) / base_mid > MAX_BASE_DRIFT_PCT:
        return None
    reference_volume = _mean([candle.volume for candle in base])
    if reference_volume <= 0:
        return None
    if max(candle.volume for candle in base) > reference_volume * MAX_BASE_VOLUME_VARIATION_RATIO:
        return None
    base_end_timestamp = base[-1].timestamp
    return BaseSetup(
        pair=pair,
        base_end_timestamp=base_end_timestamp,
        base_high=base_high,
        base_low=base_low,
        reference_volume=reference_volume,
        expires_at=base_end_timestamp + BASE_INTERVAL_MS + (SETUP_EXPIRY_SECONDS * 1000),
    )


def restore_setup(raw: dict[str, Any]) -> BaseSetup | None:
    try:
        return BaseSetup(
            pair=str(raw["pair"]),
            base_end_timestamp=int(raw["base_end_timestamp"]),
            base_high=float(raw["base_high"]),
            base_low=float(raw["base_low"]),
            reference_volume=float(raw["reference_volume"]),
            expires_at=int(raw["expires_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_trigger(pair: str, setup: BaseSetup, trigger_candle: Candle, now_ms: int | None = None) -> SignalEvaluation:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if now > setup.expires_at:
        return SignalEvaluation(None, "setup_expired")
    if trigger_candle.volume < setup.reference_volume * TRIGGER_VOLUME_MULTIPLE:
        return SignalEvaluation(None, "volume_spike_too_small")
    volume_ratio = trigger_candle.volume / setup.reference_volume
    if trigger_candle.high > setup.base_high or trigger_candle.close > setup.base_high:
        return SignalEvaluation(Signal(pair, "BUY", trigger_candle, setup, volume_ratio))
    if trigger_candle.low < setup.base_low or trigger_candle.close < setup.base_low:
        return SignalEvaluation(Signal(pair, "SELL", trigger_candle, setup, volume_ratio))
    return SignalEvaluation(None, "no_base_break")


def is_opposite_invalidated(setup: BaseSetup, trigger: Candle) -> bool:
    return trigger.low < setup.base_low or trigger.high > setup.base_high


def _evaluate_at(pair: str, base_candles: list[Candle], trigger_candles: list[Candle]) -> SignalEvaluation:
    setup = find_latest_sideways_base(pair, base_candles)
    if setup is None:
        return SignalEvaluation(None, "no_sideways_base")
    if not trigger_candles:
        return SignalEvaluation(None, "no_trigger_candle")
    return evaluate_trigger(pair, setup, trigger_candles[-1])


def evaluate_signal(pair: str, base_candles: list[Candle], trigger_candles: list[Candle]) -> SignalEvaluation:
    return _evaluate_at(pair, base_candles, trigger_candles)


def detect_signal(pair: str, base_candles: list[Candle], trigger_candles: list[Candle]) -> Signal | None:
    return evaluate_signal(pair, base_candles, trigger_candles).signal


def find_signal(pair: str, base_candles: list[Candle], trigger_candles: list[Candle]) -> Signal | None:
    return evaluate_signal(pair, base_candles, trigger_candles).signal


def format_alert(signal: Signal) -> str:
    scan_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time()))
    return (
        f"CoinDCX 15m {signal.side} alert\n"
        f"Pair: {alert_pair_name(signal.pair)}\n"
        f"Scan time: {scan_time}\n"
        f"Base: 15m sideways ending {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime((signal.setup.base_end_timestamp + BASE_INTERVAL_MS) / 1000))}\n"
        f"Base high: {signal.setup.base_high:g}\n"
        f"Base low: {signal.setup.base_low:g}\n"
        f"Reference volume: {signal.setup.reference_volume:g}\n"
        f"15m price: {signal.candle.close:g}\n"
        f"15m volume: {signal.candle.volume:g} ({signal.volume_ratio:.2f}x reference)"
    )


def send_telegram(session: requests.Session, message: str) -> bool:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        LOGGER.info("BOT_TOKEN or CHAT_ID missing; dry scan only, alert not sent")
        return False
    response = session.post(
        TELEGRAM_URL.format(token=token),
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return True


def run() -> None:
    session = requests.Session()
    state = load_state()
    sent_alerts: set[str] = state["sent_alerts"]
    setups = {pair: setup for pair, raw in state["setups"].items() if (setup := restore_setup(raw)) is not None}

    pairs: list[str] = []
    last_pair_refresh = 0.0

    while True:
        if not pairs or time.time() - last_pair_refresh >= PAIR_REFRESH_SECONDS:
            pairs = get_usdt_pairs(session)
            last_pair_refresh = time.time()
            LOGGER.info("refreshed active CoinDCX USDT pair universe: %s", len(pairs))

        valid_candle_data = 0
        too_few_candles = 0
        signals: list[Signal] = []
        alerts_sent = 0
        now_ms = int(time.time() * 1000)

        for pair in pairs:
            if pair in INVALID_CANDLE_PAIRS:
                continue
            try:
                base_candles = get_candles(session, pair, BASE_INTERVAL)
            except requests.RequestException as exc:
                if is_http_422(exc):
                    remember_invalid_candle_pair(pair, BASE_INTERVAL)
                else:
                    LOGGER.warning("%s 15m candle fetch failed: %s", pair, exc)
                continue

            latest_base = find_latest_sideways_base(pair, base_candles)
            if latest_base and (pair not in setups or latest_base.base_end_timestamp > setups[pair].base_end_timestamp):
                setups[pair] = latest_base

            setup = setups.get(pair)
            if setup is None:
                too_few_candles += 1
                continue
            if now_ms > setup.expires_at:
                setups.pop(pair, None)
                continue

            valid_candle_data += 1
            if not base_candles:
                continue

            evaluation = evaluate_trigger(pair, setup, base_candles[-1], now_ms)
            signal = evaluation.signal
            if signal is not None:
                signals.append(signal)
                if signal.alert_key in sent_alerts:
                    LOGGER.info("duplicate alert skipped: %s", signal.alert_key)
                    setups.pop(pair, None)
                elif send_telegram(session, format_alert(signal)):
                    alerts_sent += 1
                    sent_alerts.add(signal.alert_key)
                    setups.pop(pair, None)
                    save_state(sent_alerts, setups)

            if SCAN_SLEEP_SECONDS > 0:
                time.sleep(SCAN_SLEEP_SECONDS)

        save_state(sent_alerts, setups)
        buy_count = sum(1 for signal in signals if signal.side == "BUY")
        sell_count = sum(1 for signal in signals if signal.side == "SELL")
        LOGGER.info("pairs with valid candidate setups: %s", valid_candle_data)
        LOGGER.info("pairs without candidate setups: %s", too_few_candles)
        LOGGER.info("valid BUY/SELL signals found: BUY=%s SELL=%s TOTAL=%s", buy_count, sell_count, len(signals))
        LOGGER.info("Telegram alerts sent: %s", alerts_sent)

        if not RUN_FOREVER:
            break
        time.sleep(max(1.0, SCAN_SLEEP_SECONDS))

if __name__ == "__main__":
    run()
