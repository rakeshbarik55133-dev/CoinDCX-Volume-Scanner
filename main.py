"""CoinDCX USDT 15-minute photo-style breakout/breakdown Telegram scanner."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any

import requests

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
COINDCX_CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
CANDLE_LIMIT = 80
REQUEST_TIMEOUT = 20
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.15"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))
ALERT_PAIR_NAMES: dict[str, str] = {}

# Photo-style setup only: a true dead/quiet-volume, flat price base must be
# selected first. Only then can a break of that base high/low during the next
# 1 to 5 closed 15m candles alert. No RSI/EMA/MACD/ATR/BB or other indicators.
BASE_LOOKBACK = 12
DEAD_VOLUME_LOOKBACK = 8
OLDER_VOLUME_LOOKBACK = 8
MIN_HISTORY = BASE_LOOKBACK + OLDER_VOLUME_LOOKBACK
MIN_CONFIRMATION_CANDLES = 1
MAX_CONFIRMATION_CANDLES = 5

MAX_BASE_RANGE_PCT = 0.018
MAX_BASE_DRIFT_PCT = 0.008
MAX_DEAD_TO_OLDER_VOLUME_RATIO = 0.6
MAX_BASE_VOLUME_VARIATION_RATIO = 1.8
MIN_BREAK_DISTANCE_PCT = 0.002
MIN_BREAK_VOLUME_RATIO = 2.0
MAX_CONFIRMATION_EXTENSION_PCT = 0.12

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
class Signal:
    pair: str
    side: str
    candle: Candle
    average_volume: float
    volume_ratio: float
    break_level: float

    @property
    def alert_key(self) -> str:
        return f"{self.pair}:{self.side}:{self.candle.timestamp}"

    @property
    def direction(self) -> str:
        return "BREAKOUT" if self.side == "BUY" else "BREAKDOWN"

    @property
    def spike_ratio(self) -> float:
        return self.volume_ratio

    @property
    def confirmation_candle(self) -> Candle:
        return self.candle


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


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Could not read alert state; starting with empty state")
        return set()
    return set(data.get("sent_alerts", []))


def save_state(sent_alerts: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"sent_alerts": sorted(sent_alerts)}, indent=2),
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
            pair_text = str(pair)
            pairs.add(pair_text)
            ALERT_PAIR_NAMES[pair_text] = coindcx_alert_pair_name(market, pair_text)
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

    candle = Candle(
        timestamp=normalize_timestamp(timestamp),
        open=as_float(open_price),
        high=as_float(high),
        low=as_float(low),
        close=as_float(close),
        volume=as_float(volume),
    )
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


def get_candles(session: requests.Session, pair: str, log_response: bool = False) -> list[Candle]:
    response = session.get(
        COINDCX_CANDLES_URL,
        params={"pair": pair, "interval": INTERVAL, "limit": CANDLE_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    if log_response:
        LOGGER.info("%s candle response status=%s body=%s", pair, getattr(response, "status_code", "unknown"), getattr(response, "text", ""))
    response.raise_for_status()
    return parse_candles(response.json())


def get_closed_candles(candles: list[Candle]) -> list[Candle]:
    now_ms = int(time.time() * 1000)
    return [candle for candle in candles if candle.timestamp + INTERVAL_MS <= now_ms]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _evaluate_dead_volume_base(pair: str, candles: list[Candle], base_end_index: int) -> SignalEvaluation:
    if base_end_index < MIN_HISTORY - 1:
        return SignalEvaluation(None, "not_enough_history")

    base_start = base_end_index - BASE_LOOKBACK + 1
    older_start = base_start - OLDER_VOLUME_LOOKBACK
    base = candles[base_start : base_end_index + 1]
    older = candles[older_start:base_start]

    base_high = max(candle.high for candle in base)
    base_low = min(candle.low for candle in base)
    base_mid = (base_high + base_low) / 2
    if base_mid <= 0 or (base_high - base_low) / base_mid > MAX_BASE_RANGE_PCT:
        return SignalEvaluation(None, "base_not_flat")

    base_drift = abs(base[-1].close - base[0].open) / base_mid
    if base_drift > MAX_BASE_DRIFT_PCT:
        return SignalEvaluation(None, "base_trending")

    quiet_base = base[-DEAD_VOLUME_LOOKBACK:]
    base_volume = _mean([candle.volume for candle in quiet_base])
    older_volume = _mean([candle.volume for candle in older[-OLDER_VOLUME_LOOKBACK:]])
    if base_volume <= 0:
        return SignalEvaluation(None, "base_volume_zero")
    if older_volume <= 0:
        return SignalEvaluation(None, "older_volume_zero")
    if base_volume > older_volume * MAX_DEAD_TO_OLDER_VOLUME_RATIO:
        return SignalEvaluation(None, "base_volume_not_dead")
    if max(candle.volume for candle in quiet_base) > base_volume * MAX_BASE_VOLUME_VARIATION_RATIO:
        return SignalEvaluation(None, "base_volume_not_consistent")

    return SignalEvaluation(
        Signal(pair=pair, side="BASE", candle=base[-1], average_volume=base_volume, volume_ratio=1.0, break_level=base_high)
    )


def _break_signal_from_base(
    pair: str,
    trigger: Candle,
    base_high: float,
    base_low: float,
    base_volume: float,
) -> SignalEvaluation:
    volume_ratio = trigger.volume / base_volume
    if volume_ratio < MIN_BREAK_VOLUME_RATIO:
        return SignalEvaluation(None, "volume_spike_too_small")

    if trigger.close > base_high:
        if (trigger.close - base_high) / base_high < MIN_BREAK_DISTANCE_PCT:
            return SignalEvaluation(None, "weak_breakout")
        if (trigger.close - base_high) / base_high > MAX_CONFIRMATION_EXTENSION_PCT:
            return SignalEvaluation(None, "trigger_extended")
        return SignalEvaluation(Signal(pair, "BUY", trigger, base_volume, volume_ratio, base_high))

    if trigger.close < base_low:
        if (base_low - trigger.close) / base_low < MIN_BREAK_DISTANCE_PCT:
            return SignalEvaluation(None, "weak_breakdown")
        if (base_low - trigger.close) / base_low > MAX_CONFIRMATION_EXTENSION_PCT:
            return SignalEvaluation(None, "trigger_extended")
        return SignalEvaluation(Signal(pair, "SELL", trigger, base_volume, volume_ratio, base_low))

    return SignalEvaluation(None, "no_base_break")


def _evaluate_at(pair: str, candles: list[Candle], index: int) -> SignalEvaluation:
    if index < MIN_HISTORY:
        return SignalEvaluation(None, "not_enough_history")

    last_rejection = "no_dead_volume_base"
    for candles_after_base in range(MIN_CONFIRMATION_CANDLES, MAX_CONFIRMATION_CANDLES + 1):
        base_end_index = index - candles_after_base
        base_evaluation = _evaluate_dead_volume_base(pair, candles, base_end_index)
        if base_evaluation.signal is None:
            last_rejection = base_evaluation.rejection_reason or last_rejection
            continue

        base_start = base_end_index - BASE_LOOKBACK + 1
        base = candles[base_start : base_end_index + 1]
        base_high = max(candle.high for candle in base)
        base_low = min(candle.low for candle in base)
        base_volume = base_evaluation.signal.average_volume

        # Expire this dead-volume setup if any earlier confirmation candle already
        # broke either side; the scanner alerts only on the first 1-5 candle break.
        prior_confirmation = candles[base_end_index + 1 : index]
        if any(candle.close > base_high or candle.close < base_low for candle in prior_confirmation):
            last_rejection = "setup_already_broke"
            continue

        return _break_signal_from_base(pair, candles[index], base_high, base_low, base_volume)

    return SignalEvaluation(None, last_rejection)


def evaluate_signal(pair: str, candles: list[Candle]) -> SignalEvaluation:
    if len(candles) < MIN_HISTORY + MIN_CONFIRMATION_CANDLES:
        return SignalEvaluation(None, "not_enough_history")
    return _evaluate_at(pair, candles, len(candles) - 1)

def detect_signal(pair: str, candles: list[Candle]) -> Signal | None:
    return evaluate_signal(pair, candles).signal


def find_signal(pair: str, candles: list[Candle]) -> Signal | None:
    closed = get_closed_candles(candles)
    if len(closed) < MIN_HISTORY + 1:
        return None
    # Live scanner alerts only when the newest closed candle is the first photo-style
    # breakout/breakdown candle, preventing late/extra alerts after the move extends.
    return _evaluate_at(pair, closed, len(closed) - 1).signal


def format_alert(signal: Signal) -> str:
    candle_close_time = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC",
        time.gmtime((signal.candle.timestamp + INTERVAL_MS) / 1000),
    )
    return (
        f"CoinDCX 15m {signal.side} signal\n"
        f"Pair: {alert_pair_name(signal.pair)}\n"
        f"Candle closed: {candle_close_time}\n"
        f"Close: {signal.candle.close:g}\n"
        f"Volume: {signal.candle.volume:g} ({signal.volume_ratio:.2f}x quiet-base average)\n"
        f"Break level: {signal.break_level:g}"
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
    sent_alerts = load_state()

    pairs = get_usdt_pairs(session)
    LOGGER.info("total CoinDCX tradable USDT pairs found: %s", len(pairs))

    valid_candle_data = 0
    too_few_candles = 0
    signals: list[Signal] = []
    alerts_sent = 0

    for pair in pairs:
        try:
            candles = get_candles(session, pair)
        except requests.RequestException as exc:
            LOGGER.warning("%s candle fetch failed: %s", pair, exc)
            continue

        if len(get_closed_candles(candles)) < MIN_HISTORY + 1:
            too_few_candles += 1
            continue

        valid_candle_data += 1
        signal = find_signal(pair, candles)
        if signal is not None:
            signals.append(signal)
            if signal.alert_key in sent_alerts:
                LOGGER.info("duplicate alert skipped: %s", signal.alert_key)
            elif send_telegram(session, format_alert(signal)):
                alerts_sent += 1
                sent_alerts.add(signal.alert_key)
                save_state(sent_alerts)

        if SCAN_SLEEP_SECONDS > 0:
            time.sleep(SCAN_SLEEP_SECONDS)

    buy_count = sum(1 for signal in signals if signal.side == "BUY")
    sell_count = sum(1 for signal in signals if signal.side == "SELL")
    LOGGER.info("pairs with valid candle data: %s", valid_candle_data)
    LOGGER.info("too_few_candles count: %s", too_few_candles)
    LOGGER.info("valid BUY/SELL signals found: BUY=%s SELL=%s TOTAL=%s", buy_count, sell_count, len(signals))
    LOGGER.info("Telegram alerts sent: %s", alerts_sent)


if __name__ == "__main__":
    run()
