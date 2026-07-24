"""Standalone stock scanner with independent Telegram alerts.

This file is intentionally separate from main.py so the existing CoinDCX crypto
scanner and strategy can continue to run unchanged. Run with:

    python stock_main.py

Configuration is controlled with STOCK_* environment variables only:
- STOCK_SYMBOLS: comma-separated tickers to scan (default: SPY,QQQ,AAPL,MSFT,NVDA,TSLA)
- STOCK_BOT_TOKEN / STOCK_CHAT_ID: Telegram destination for stock alerts
- STOCK_STATE_FILE: duplicate-alert state file (default: .stock_alert_state.json)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

INTERVAL = os.getenv("STOCK_INTERVAL", "15m")
RANGE = os.getenv("STOCK_RANGE", "5d")
REQUEST_TIMEOUT = 20
SCAN_SLEEP_SECONDS = float(os.getenv("STOCK_SCAN_SLEEP_SECONDS", "0.15"))
FULL_SCAN_DELAY_SECONDS = 5 * 60
STATE_FILE = Path(os.getenv("STOCK_STATE_FILE", ".stock_alert_state.json"))
DEFAULT_SYMBOLS = "SPY,QQQ,AAPL,MSFT,NVDA,TSLA"
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_TIME = datetime_time(9, 0)
MARKET_CLOSE_TIME = datetime_time(15, 30)

# Separate stock strategy: quiet consolidation followed by a closing break on
# above-average volume. These constants intentionally do not import or share any
# CoinDCX crypto scanner logic or state.
BASE_LOOKBACK = 12
OLDER_VOLUME_LOOKBACK = 12
MIN_HISTORY = BASE_LOOKBACK + OLDER_VOLUME_LOOKBACK
MAX_BASE_RANGE_PCT = 0.025
MAX_BASE_DRIFT_PCT = 0.012
MAX_QUIET_TO_OLDER_VOLUME_RATIO = 0.75
MIN_BREAK_DISTANCE_PCT = 0.0015
MIN_BREAK_VOLUME_RATIO = 1.8
MAX_BREAK_EXTENSION_PCT = 0.08

logging.basicConfig(
    level=os.getenv("STOCK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockCandle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class StockSignal:
    symbol: str
    side: str
    candle: StockCandle
    quiet_volume_average: float
    volume_ratio: float
    break_level: float

    @property
    def alert_key(self) -> str:
        return f"{self.symbol}:{self.side}:{self.candle.timestamp}"

    @property
    def direction(self) -> str:
        return "BREAKOUT" if self.side == "BUY" else "BREAKDOWN"


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def configured_symbols() -> list[str]:
    raw_symbols = os.getenv("STOCK_SYMBOLS", DEFAULT_SYMBOLS)
    symbols = [symbol.strip().upper() for symbol in raw_symbols.split(",")]
    return sorted({symbol for symbol in symbols if symbol})


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Could not read stock alert state; starting with empty state")
        return set()
    return set(data.get("sent_alerts", []))


def save_state(sent_alerts: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"sent_alerts": sorted(sent_alerts)}, indent=2),
        encoding="utf-8",
    )


def parse_yahoo_chart(payload: dict[str, Any]) -> list[StockCandle]:
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return []

    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    candles: list[StockCandle] = []
    for index, timestamp in enumerate(timestamps):
        candle = StockCandle(
            timestamp=int(as_float(timestamp)) * 1000,
            open=as_float(opens[index] if index < len(opens) else None),
            high=as_float(highs[index] if index < len(highs) else None),
            low=as_float(lows[index] if index < len(lows) else None),
            close=as_float(closes[index] if index < len(closes) else None),
            volume=as_float(volumes[index] if index < len(volumes) else None),
        )
        if candle.timestamp > 0 and candle.high > 0 and candle.low > 0 and candle.close > 0:
            candles.append(candle)
    return sorted(candles, key=lambda candle: candle.timestamp)


def get_stock_candles(session: requests.Session, symbol: str) -> list[StockCandle]:
    response = session.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"interval": INTERVAL, "range": RANGE, "includePrePost": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return parse_yahoo_chart(response.json())


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def detect_stock_signal(symbol: str, candles: list[StockCandle]) -> StockSignal | None:
    if len(candles) < MIN_HISTORY + 1:
        return None

    base = candles[-BASE_LOOKBACK - 1 : -1]
    older = candles[-MIN_HISTORY - 1 : -BASE_LOOKBACK - 1]
    trigger = candles[-1]

    base_high = max(candle.high for candle in base)
    base_low = min(candle.low for candle in base)
    base_mid = (base_high + base_low) / 2
    if base_mid <= 0 or (base_high - base_low) / base_mid > MAX_BASE_RANGE_PCT:
        return None
    if abs(base[-1].close - base[0].open) / base_mid > MAX_BASE_DRIFT_PCT:
        return None

    base_volume = mean([candle.volume for candle in base])
    older_volume = mean([candle.volume for candle in older])
    if base_volume <= 0 or older_volume <= 0:
        return None
    if base_volume > older_volume * MAX_QUIET_TO_OLDER_VOLUME_RATIO:
        return None

    volume_ratio = trigger.volume / base_volume
    if volume_ratio < MIN_BREAK_VOLUME_RATIO:
        return None

    if trigger.close > base_high:
        break_distance = (trigger.close - base_high) / base_high
        if MIN_BREAK_DISTANCE_PCT <= break_distance <= MAX_BREAK_EXTENSION_PCT:
            return StockSignal(symbol, "BUY", trigger, base_volume, volume_ratio, base_high)
    elif trigger.close < base_low:
        break_distance = (base_low - trigger.close) / base_low
        if MIN_BREAK_DISTANCE_PCT <= break_distance <= MAX_BREAK_EXTENSION_PCT:
            return StockSignal(symbol, "SELL", trigger, base_volume, volume_ratio, base_low)

    return None


def format_stock_alert(signal: StockSignal) -> str:
    candle_close_time = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC",
        time.gmtime(signal.candle.timestamp / 1000),
    )
    return (
        f"Stock 15m {signal.side} signal\n"
        f"Symbol: {signal.symbol}\n"
        f"Direction: {signal.direction}\n"
        f"Candle time: {candle_close_time}\n"
        f"Close: {signal.candle.close:g}\n"
        f"Volume: {signal.candle.volume:g} ({signal.volume_ratio:.2f}x quiet-base average)\n"
        f"Break level: {signal.break_level:g}"
    )


def send_stock_telegram(session: requests.Session, message: str) -> bool:
    token = os.getenv("STOCK_BOT_TOKEN")
    chat_id = os.getenv("STOCK_CHAT_ID")
    if not token or not chat_id:
        LOGGER.info("STOCK_BOT_TOKEN or STOCK_CHAT_ID missing; dry stock scan only, alert not sent")
        return False

    response = session.post(
        TELEGRAM_URL.format(token=token),
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return True


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(moment: datetime | None = None) -> bool:
    current = moment or now_ist()
    current_time = current.timetz().replace(tzinfo=None)
    return MARKET_OPEN_TIME <= current_time < MARKET_CLOSE_TIME


def next_market_open(moment: datetime | None = None) -> datetime:
    current = moment or now_ist()
    next_open = current.replace(
        hour=MARKET_OPEN_TIME.hour,
        minute=MARKET_OPEN_TIME.minute,
        second=0,
        microsecond=0,
    )
    if current >= next_open:
        next_open += timedelta(days=1)
    return next_open


def sleep_until_market_open() -> None:
    next_open = next_market_open()
    sleep_seconds = max(0.0, (next_open - now_ist()).total_seconds())
    LOGGER.info(
        "outside Indian market hours; waiting until %s IST",
        next_open.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def run_scan(session: requests.Session, sent_alerts: set[str], symbols: list[str]) -> None:
    if not is_market_open():
        LOGGER.info("market closed before stock scan started; skipping fetches")
        return

    signals: list[StockSignal] = []
    alerts_sent = 0
    for symbol in symbols:
        if not is_market_open():
            LOGGER.info("market closed during stock scan; stopping before fetching %s", symbol)
            break

        try:
            candles = get_stock_candles(session, symbol)
        except requests.RequestException as exc:
            LOGGER.warning("%s stock candle fetch failed: %s", symbol, exc)
            continue

        signal = detect_stock_signal(symbol, candles)
        if signal is not None:
            signals.append(signal)
            if signal.alert_key in sent_alerts:
                LOGGER.info("duplicate stock alert skipped: %s", signal.alert_key)
            elif send_stock_telegram(session, format_stock_alert(signal)):
                alerts_sent += 1
                sent_alerts.add(signal.alert_key)
                save_state(sent_alerts)

        if SCAN_SLEEP_SECONDS > 0:
            time.sleep(SCAN_SLEEP_SECONDS)

    buy_count = sum(1 for signal in signals if signal.side == "BUY")
    sell_count = sum(1 for signal in signals if signal.side == "SELL")
    LOGGER.info("stock BUY/SELL signals found: BUY=%s SELL=%s TOTAL=%s", buy_count, sell_count, len(signals))
    LOGGER.info("stock Telegram alerts sent: %s", alerts_sent)


def run() -> None:
    session = requests.Session()
    sent_alerts = load_state()
    symbols = configured_symbols()
    LOGGER.info("total stock symbols configured: %s", len(symbols))

    while True:
        if not is_market_open():
            sleep_until_market_open()
            continue

        run_scan(session, sent_alerts, symbols)

        next_scan_at = now_ist() + timedelta(seconds=FULL_SCAN_DELAY_SECONDS)
        if not is_market_open(next_scan_at):
            sleep_until_market_open()
        else:
            LOGGER.info("next stock scan starts at %s IST", next_scan_at.strftime("%Y-%m-%d %H:%M:%S"))
            time.sleep(FULL_SCAN_DELAY_SECONDS)


if __name__ == "__main__":
    run()
