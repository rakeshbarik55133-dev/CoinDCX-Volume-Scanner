"""CoinDCX USDT volume breakout Telegram scanner.

Scans CoinDCX USDT markets on the 15-minute timeframe, looking for a quiet
("dead volume") window followed by a relative volume spike and a confirmed
price breakout or breakdown. Alerts are sent to Telegram and de-duplicated per
pair, direction, and candle close time.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/market_details"
COINDCX_CANDLES_URL = "https://public.coindcx.com/market_data/candles/"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

INTERVAL = "15m"
CANDLE_LIMIT = 80
REQUEST_TIMEOUT = 20
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))

DEAD_VOLUME_LOOKBACK = int(os.getenv("DEAD_VOLUME_LOOKBACK", "8"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "12"))
DEAD_VOLUME_RATIO = float(os.getenv("DEAD_VOLUME_RATIO", "0.65"))
SPIKE_MULTIPLIER = float(os.getenv("SPIKE_MULTIPLIER", "2.5"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "1000"))
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.25"))
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "0"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: int

    @property
    def quote_volume(self) -> float:
        return self.close * self.volume


@dataclass(frozen=True)
class Signal:
    pair: str
    direction: str
    candle: Candle
    dead_average: float
    previous_average: float
    breakout_level: float
    spike_ratio: float

    @property
    def alert_key(self) -> str:
        return f"{self.pair}:{self.direction}:{self.candle.timestamp}"


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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


def get_usdt_pairs(session: requests.Session) -> list[str]:
    response = session.get(COINDCX_MARKETS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    markets = response.json()

    pairs: list[str] = []
    for market in markets:
        if not market.get("coindcx_name"):
            continue
        if market.get("base_currency_short_name") != "USDT":
            continue
        if market.get("status", "active").lower() not in {"active", "online"}:
            continue
        pairs.append(str(market["coindcx_name"]))

    unique_pairs = sorted(set(pairs))
    if MAX_PAIRS > 0:
        return unique_pairs[:MAX_PAIRS]
    return unique_pairs


def normalize_candle(raw: dict[str, Any]) -> Candle:
    return Candle(
        open=as_float(raw.get("open")),
        high=as_float(raw.get("high")),
        low=as_float(raw.get("low")),
        close=as_float(raw.get("close")),
        volume=as_float(raw.get("volume")),
        timestamp=int(as_float(raw.get("time") or raw.get("timestamp"))),
    )


def get_candles(session: requests.Session, pair: str) -> list[Candle]:
    response = session.get(
        COINDCX_CANDLES_URL,
        params={"pair": pair, "interval": INTERVAL, "limit": CANDLE_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    candles = [normalize_candle(item) for item in response.json()]
    valid_candles = (candle for candle in candles if candle.timestamp)
    return sorted(valid_candles, key=lambda item: item.timestamp)


def average(values: Iterable[float]) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else 0.0


def detect_signal(pair: str, candles: list[Candle]) -> Signal | None:
    minimum_needed = DEAD_VOLUME_LOOKBACK + BREAKOUT_LOOKBACK + 2
    if len(candles) < minimum_needed:
        return None

    closed_candles = candles[:-1]
    spike_candle = closed_candles[-1]
    dead_window = closed_candles[-(DEAD_VOLUME_LOOKBACK + 1) : -1]
    history = closed_candles[: -(DEAD_VOLUME_LOOKBACK + 1)]
    breakout_window = closed_candles[-(BREAKOUT_LOOKBACK + 1) : -1]

    if len(dead_window) < DEAD_VOLUME_LOOKBACK or len(history) < BREAKOUT_LOOKBACK:
        return None

    dead_average = average(candle.volume for candle in dead_window)
    previous_average = average(candle.volume for candle in history[-BREAKOUT_LOOKBACK:])
    if previous_average <= 0 or dead_average <= 0:
        return None

    is_dead_volume = dead_average <= previous_average * DEAD_VOLUME_RATIO
    is_spike = spike_candle.volume >= dead_average * SPIKE_MULTIPLIER
    has_minimum_liquidity = spike_candle.quote_volume >= MIN_QUOTE_VOLUME
    if not (is_dead_volume and is_spike and has_minimum_liquidity):
        return None

    prior_high = max(candle.high for candle in breakout_window)
    prior_low = min(candle.low for candle in breakout_window)

    if spike_candle.close > prior_high:
        return Signal(
            pair=pair,
            direction="BREAKOUT",
            candle=spike_candle,
            dead_average=dead_average,
            previous_average=previous_average,
            breakout_level=prior_high,
            spike_ratio=spike_candle.volume / dead_average,
        )
    if spike_candle.close < prior_low:
        return Signal(
            pair=pair,
            direction="BREAKDOWN",
            candle=spike_candle,
            dead_average=dead_average,
            previous_average=previous_average,
            breakout_level=prior_low,
            spike_ratio=spike_candle.volume / dead_average,
        )
    return None


def format_alert(signal: Signal) -> str:
    emoji = "🚀" if signal.direction == "BREAKOUT" else "🔻"
    return (
        f"{emoji} CoinDCX 15m {signal.direction}\n"
        f"Pair: {signal.pair}\n"
        f"Close: {signal.candle.close:g}\n"
        f"Level: {signal.breakout_level:g}\n"
        f"Volume spike: {signal.spike_ratio:.2f}x dead-volume average\n"
        f"Dead avg volume: {signal.dead_average:.4f}\n"
        f"Previous avg volume: {signal.previous_average:.4f}\n"
        f"Quote volume: {signal.candle.quote_volume:.2f} USDT\n"
        f"Candle time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(signal.candle.timestamp / 1000))}"
    )


def send_telegram_alert(
    session: requests.Session, token: str, chat_id: str, message: str
) -> None:
    response = session.post(
        TELEGRAM_URL.format(token=token),
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def run_scan() -> int:
    bot_token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not bot_token or not chat_id:
        raise RuntimeError("BOT_TOKEN and CHAT_ID environment variables are required")

    sent_alerts = load_state()
    alerts_sent = 0

    with requests.Session() as session:
        pairs = get_usdt_pairs(session)
        LOGGER.info("Scanning %s CoinDCX USDT pairs on %s", len(pairs), INTERVAL)
        for pair in pairs:
            try:
                signal = detect_signal(pair, get_candles(session, pair))
            except requests.RequestException as exc:
                LOGGER.warning("Skipping %s after API error: %s", pair, exc)
                continue

            if not signal or signal.alert_key in sent_alerts:
                time.sleep(SCAN_SLEEP_SECONDS)
                continue

            send_telegram_alert(session, bot_token, chat_id, format_alert(signal))
            sent_alerts.add(signal.alert_key)
            alerts_sent += 1
            LOGGER.info("Sent %s alert for %s", signal.direction, pair)
            time.sleep(SCAN_SLEEP_SECONDS)

    save_state(sent_alerts)
    LOGGER.info("Scan complete; sent %s alert(s)", alerts_sent)
    return alerts_sent


if __name__ == "__main__":
    run_scan()
