"""CoinDCX USDT 15-minute Volume + Price Action Telegram scanner."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
COINDCX_CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
LOOKBACK = 20
VOLUME_MULTIPLIER = 3.0
CANDLE_LIMIT = 60
REQUEST_TIMEOUT = 20
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.15"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))

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


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_timestamp(value: Any) -> int:
    timestamp = int(as_float(value))
    if 0 < timestamp < 10_000_000_000:
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


def is_tradable_spot_usdt_market(market: dict[str, Any]) -> bool:
    pair = str(market.get("pair") or "").upper()
    status = str(market.get("status") or "").lower()
    base_currency = str(market.get("base_currency_short_name") or "").upper()

    # CoinDCX market details defines `pair` as the exact market identifier to
    # use with public market-data endpoints. For USDT spot scans, only keep
    # live markets whose quote/base currency is USDT and whose pair is present
    # in the live market details response, e.g. B-BTC_USDT. This prevents
    # alerts for display symbols or reverse INR pairs such as USDT_INR.
    if not pair or not pair.endswith("_USDT"):
        return False
    if base_currency != "USDT":
        return False
    if status != "active":
        return False

    market_type = str(market.get("market_type") or market.get("segment") or "spot").lower()
    return market_type == "spot"


def get_usdt_pairs(session: requests.Session) -> list[str]:
    response = session.get(COINDCX_MARKETS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    pairs: set[str] = set()
    for market in response.json():
        if is_tradable_spot_usdt_market(market):
            pairs.add(str(market["pair"]))
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


def get_candles(session: requests.Session, pair: str) -> list[Candle]:
    response = session.get(
        COINDCX_CANDLES_URL,
        params={"pair": pair, "interval": INTERVAL, "limit": CANDLE_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return parse_candles(response.json())


def get_closed_candles(candles: list[Candle]) -> list[Candle]:
    now_ms = int(time.time() * 1000)
    return [candle for candle in candles if candle.timestamp + INTERVAL_MS <= now_ms]


def find_signal(pair: str, candles: list[Candle]) -> Signal | None:
    closed = get_closed_candles(candles)
    if len(closed) < LOOKBACK + 1:
        return None

    latest = closed[-1]
    previous = closed[-(LOOKBACK + 1):-1]
    average_volume = sum(candle.volume for candle in previous) / LOOKBACK
    if average_volume <= 0 or latest.volume < average_volume * VOLUME_MULTIPLIER:
        return None

    previous_high = max(candle.high for candle in previous)
    previous_low = min(candle.low for candle in previous)

    if latest.close > latest.open and latest.close > previous_high:
        return Signal(pair, "BUY", latest, average_volume, latest.volume / average_volume, previous_high)
    if latest.close < latest.open and latest.close < previous_low:
        return Signal(pair, "SELL", latest, average_volume, latest.volume / average_volume, previous_low)
    return None


def format_alert(signal: Signal) -> str:
    candle_close_time = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC",
        time.gmtime((signal.candle.timestamp + INTERVAL_MS) / 1000),
    )
    return (
        f"CoinDCX 15m {signal.side} signal\n"
        f"Pair: {signal.pair}\n"
        f"Candle closed: {candle_close_time}\n"
        f"Close: {signal.candle.close:g}\n"
        f"Volume: {signal.candle.volume:g} "
        f"({signal.volume_ratio:.2f}x 20-candle average)\n"
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
    LOGGER.info("total USDT pairs found: %s", len(pairs))

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

        if len(get_closed_candles(candles)) < LOOKBACK + 1:
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
    LOGGER.info(
        "valid BUY/SELL signals found: BUY=%s SELL=%s TOTAL=%s",
        buy_count,
        sell_count,
        len(signals),
    )
    LOGGER.info("Telegram alerts sent: %s", alerts_sent)


if __name__ == "__main__":
    run()
