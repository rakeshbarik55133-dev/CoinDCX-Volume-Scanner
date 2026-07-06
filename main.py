"""FYERS NSE Futures 15-minute volume breakout Telegram scanner.

The bot scans FYERS NSE Futures instruments, checks the latest closed
15-minute candle against the previous 20 closed candles, and sends de-duplicated
Telegram alerts for high-volume breakouts/breakdowns.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Iterable

import requests

FYERS_NSE_FO_SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

TIMEFRAME_MINUTES = 15
FYERS_RESOLUTION = str(TIMEFRAME_MINUTES)
LOOKBACK_CANDLES = int(os.getenv("LOOKBACK_CANDLES", "20"))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "3"))
RISK_REWARD = float(os.getenv("RISK_REWARD", "2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.15"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "0"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))

# Need at least 20 previous closed candles plus the current/latest closed candle.
CANDLE_DAYS_BACK = int(os.getenv("CANDLE_DAYS_BACK", "10"))

REQUIRED_ENV_VARS = (
    "FYERS_APP_ID",
    "FYERS_SECRET_KEY",
    "FYERS_REDIRECT_URI",
    "FYERS_ACCESS_TOKEN",
    "BOT_TOKEN",
    "CHAT_ID",
)

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
    symbol: str
    direction: str
    candle: Candle
    average_volume: float
    previous_high: float
    previous_low: float
    signal_strength: float
    stop_loss: float
    target: float

    @property
    def alert_key(self) -> str:
        return f"{self.symbol}:{self.direction}:{self.candle.timestamp}"


def get_required_env() -> dict[str, str]:
    values = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return values


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


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def average(values: Iterable[float]) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else 0.0


def get_nse_futures_symbols(session: requests.Session) -> list[str]:
    """Return all FYERS NSE futures symbols from the FYERS symbol master."""
    response = session.get(FYERS_NSE_FO_SYMBOL_MASTER_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    symbols: set[str] = set()
    reader = csv.reader(StringIO(response.text))
    for row in reader:
        fyers_symbols = [item.strip() for item in row if item.strip().startswith("NSE:")]
        for symbol in fyers_symbols:
            if "FUT" in symbol.upper():
                symbols.add(symbol)

    ordered = sorted(symbols)
    if MAX_SYMBOLS > 0:
        return ordered[:MAX_SYMBOLS]
    return ordered


def get_fyers_client(app_id: str, access_token: str) -> Any:
    from fyers_apiv3 import fyersModel

    return fyersModel.FyersModel(client_id=app_id, token=access_token, is_async=False, log_path="")


def get_candles(fyers: Any, symbol: str) -> list[Candle]:
    today = datetime.now(UTC).date()
    payload = {
        "symbol": symbol,
        "resolution": FYERS_RESOLUTION,
        "date_format": "1",
        "range_from": (today - timedelta(days=CANDLE_DAYS_BACK)).isoformat(),
        "range_to": today.isoformat(),
        "cont_flag": "1",
    }
    response = fyers.history(data=payload)
    if not isinstance(response, dict) or response.get("s") != "ok":
        raise RuntimeError(f"FYERS history failed for {symbol}: {response}")

    candles = []
    for raw in response.get("candles", []):
        if len(raw) < 6:
            continue
        candles.append(
            Candle(
                timestamp=int(as_float(raw[0])),
                open=as_float(raw[1]),
                high=as_float(raw[2]),
                low=as_float(raw[3]),
                close=as_float(raw[4]),
                volume=as_float(raw[5]),
            )
        )
    return sorted(candles, key=lambda item: item.timestamp)


def latest_closed_candles(candles: list[Candle]) -> list[Candle]:
    """Drop an in-progress candle when FYERS returns one for the current interval."""
    if not candles:
        return []
    now = int(time.time())
    interval_seconds = TIMEFRAME_MINUTES * 60
    if candles[-1].timestamp + interval_seconds > now:
        return candles[:-1]
    return candles


def detect_signal(symbol: str, candles: list[Candle]) -> Signal | None:
    closed = latest_closed_candles(candles)
    if len(closed) < LOOKBACK_CANDLES + 1:
        return None

    current = closed[-1]
    previous = closed[-(LOOKBACK_CANDLES + 1) : -1]
    avg_volume = average(candle.volume for candle in previous)
    if avg_volume <= 0 or current.volume < avg_volume * VOLUME_MULTIPLIER:
        return None

    previous_high = max(candle.high for candle in previous)
    previous_low = min(candle.low for candle in previous)
    strength = current.volume / avg_volume

    if current.close > current.open and current.close > previous_high:
        stop_loss = min(current.low, previous_low)
        risk = current.close - stop_loss
        target = current.close + risk * RISK_REWARD
        return Signal(symbol, "BUY", current, avg_volume, previous_high, previous_low, strength, stop_loss, target)

    if current.close < current.open and current.close < previous_low:
        stop_loss = max(current.high, previous_high)
        risk = stop_loss - current.close
        target = current.close - risk * RISK_REWARD
        return Signal(symbol, "SELL", current, avg_volume, previous_high, previous_low, strength, stop_loss, target)

    return None


def format_alert(signal: Signal) -> str:
    candle_time = datetime.fromtimestamp(signal.candle.timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    emoji = "🟢" if signal.direction == "BUY" else "🔴"
    return (
        f"{emoji} FYERS NSE Futures 15m {signal.direction}\n"
        f"Symbol: {signal.symbol}\n"
        f"Entry: {signal.candle.close:.2f}\n"
        f"Stop Loss: {signal.stop_loss:.2f}\n"
        f"Target: {signal.target:.2f}\n"
        f"Signal Strength: {signal.signal_strength:.2f}x volume\n"
        f"Current Volume: {signal.candle.volume:.0f}\n"
        f"20-Candle Avg Volume: {signal.average_volume:.0f}\n"
        f"20-Candle High: {signal.previous_high:.2f}\n"
        f"20-Candle Low: {signal.previous_low:.2f}\n"
        f"Candle Time: {candle_time}"
    )


def send_telegram_alert(session: requests.Session, token: str, chat_id: str, message: str) -> None:
    response = session.post(
        TELEGRAM_URL.format(token=token),
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def run_scan() -> int:
    env = get_required_env()
    sent_alerts = load_state()
    alerts_sent = 0

    fyers = get_fyers_client(env["FYERS_APP_ID"], env["FYERS_ACCESS_TOKEN"])
    with requests.Session() as session:
        symbols = get_nse_futures_symbols(session)
        LOGGER.info("Scanning %s FYERS NSE futures symbols on %s-minute candles", len(symbols), TIMEFRAME_MINUTES)
        for symbol in symbols:
            try:
                signal = detect_signal(symbol, get_candles(fyers, symbol))
            except Exception as exc:  # noqa: BLE001 - continue scanning after per-symbol API/data errors.
                LOGGER.warning("Skipping %s after data error: %s", symbol, exc)
                time.sleep(SCAN_SLEEP_SECONDS)
                continue

            if not signal or signal.alert_key in sent_alerts:
                time.sleep(SCAN_SLEEP_SECONDS)
                continue

            send_telegram_alert(session, env["BOT_TOKEN"], env["CHAT_ID"], format_alert(signal))
            sent_alerts.add(signal.alert_key)
            alerts_sent += 1
            LOGGER.info("Sent %s alert for %s", signal.direction, symbol)
            time.sleep(SCAN_SLEEP_SECONDS)

    save_state(sent_alerts)
    LOGGER.info("Scan complete; sent %s alert(s)", alerts_sent)
    return alerts_sent


if __name__ == "__main__":
    run_scan()
