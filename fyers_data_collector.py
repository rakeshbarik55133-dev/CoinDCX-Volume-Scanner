"""FYERS API v3 5-minute OHLCV data collector for NSE F&O equities.

The collector is intentionally data-only. It does not include screener, signal,
Telegram, or order-placement logic, and it does not import or modify the
existing CoinDCX scanner.

Configuration is read only from environment variables / GitHub Secrets:
- FYERS_ACCESS_TOKEN: required FYERS API v3 access token, used directly.
- FYERS_DB_PATH: optional SQLite database path (default: fyers_ohlcv.sqlite3).
- FYERS_SYMBOLS: optional comma-separated FYERS symbols. Defaults to a starter
  list of NSE F&O equity symbols such as NSE:RELIANCE-EQ.
- FYERS_START_DATE: optional YYYY-MM-DD first collection date when a symbol has
  no stored candles (default: yesterday UTC).
- FYERS_END_DATE: optional YYYY-MM-DD final collection date (default: today UTC).
- FYERS_SLEEP_SECONDS: optional delay between symbol requests (default: 0.2).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
RESOLUTION = "5"
REQUEST_TIMEOUT = 30
DEFAULT_DB_PATH = "fyers_ohlcv.sqlite3"
DEFAULT_SYMBOLS = (
    "NSE:RELIANCE-EQ,NSE:TCS-EQ,NSE:HDFCBANK-EQ,NSE:ICICIBANK-EQ,"
    "NSE:INFY-EQ,NSE:SBIN-EQ,NSE:AXISBANK-EQ,NSE:KOTAKBANK-EQ,"
    "NSE:LT-EQ,NSE:ITC-EQ,NSE:BHARTIARTL-EQ,NSE:MARUTI-EQ,"
    "NSE:TATAMOTORS-EQ,NSE:BAJFINANCE-EQ,NSE:HINDUNILVR-EQ"
)

logging.basicConfig(
    level=os.getenv("FYERS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FyersCandle:
    symbol: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class CollectionResult:
    symbol: str
    requested_from: str
    requested_to: str
    fetched: int
    inserted_or_updated: int


def get_access_token() -> str:
    """Return the FYERS access token without logging or transforming it."""
    token = os.getenv("FYERS_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("FYERS_ACCESS_TOKEN environment variable is required")
    return token


def configured_symbols() -> list[str]:
    raw_symbols = os.getenv("FYERS_SYMBOLS", DEFAULT_SYMBOLS)
    symbols = [symbol.strip().upper() for symbol in raw_symbols.split(",")]
    return sorted({symbol for symbol in symbols if symbol})


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def today_utc() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def default_start_date() -> dt.date:
    return today_utc() - dt.timedelta(days=1)


def configured_date_range() -> tuple[dt.date, dt.date]:
    start = parse_date(os.getenv("FYERS_START_DATE", default_start_date().isoformat()))
    end = parse_date(os.getenv("FYERS_END_DATE", today_utc().isoformat()))
    if end < start:
        raise ValueError("FYERS_END_DATE must be on or after FYERS_START_DATE")
    return start, end


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or os.getenv("FYERS_DB_PATH", DEFAULT_DB_PATH))
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS fyers_ohlcv_5m (
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, timestamp)
        )
        """
    )
    connection.commit()


def latest_timestamp(connection: sqlite3.Connection, symbol: str) -> int | None:
    row = connection.execute(
        "SELECT MAX(timestamp) FROM fyers_ohlcv_5m WHERE symbol = ?", (symbol,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def incremental_start_date(
    connection: sqlite3.Connection, symbol: str, fallback_start: dt.date
) -> dt.date:
    latest = latest_timestamp(connection, symbol)
    if latest is None:
        return fallback_start
    latest_date = dt.datetime.fromtimestamp(latest, tz=dt.UTC).date()
    return latest_date


def build_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": access_token}


def fetch_history(
    session: requests.Session,
    access_token: str,
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
) -> list[FyersCandle]:
    response = session.get(
        FYERS_HISTORY_URL,
        headers=build_headers(access_token),
        params={
            "symbol": symbol,
            "resolution": RESOLUTION,
            "date_format": "1",
            "range_from": start_date.isoformat(),
            "range_to": end_date.isoformat(),
            "cont_flag": "1",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return parse_history_response(symbol, response.json())


def parse_history_response(symbol: str, payload: dict[str, Any]) -> list[FyersCandle]:
    if str(payload.get("s", "ok")).lower() not in {"ok", "success"}:
        message = str(payload.get("message") or payload.get("errmsg") or "FYERS history request failed")
        raise RuntimeError(message)

    candles: list[FyersCandle] = []
    for raw in payload.get("candles") or []:
        if not isinstance(raw, list) or len(raw) < 6:
            continue
        timestamp, open_price, high, low, close, volume = raw[:6]
        candle = FyersCandle(
            symbol=symbol,
            timestamp=int(float(timestamp)),
            open=float(open_price),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume),
        )
        if candle.timestamp > 0 and candle.high > 0 and candle.low > 0 and candle.close > 0:
            candles.append(candle)
    return sorted(candles, key=lambda candle: candle.timestamp)


def store_candles(connection: sqlite3.Connection, candles: Iterable[FyersCandle]) -> int:
    rows = [
        (
            candle.symbol,
            candle.timestamp,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
        )
        for candle in candles
    ]
    if not rows:
        return 0
    cursor = connection.executemany(
        """
        INSERT INTO fyers_ohlcv_5m (symbol, timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, timestamp) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            updated_at = CURRENT_TIMESTAMP
        """,
        rows,
    )
    connection.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


def collect_symbol(
    connection: sqlite3.Connection,
    session: requests.Session,
    access_token: str,
    symbol: str,
    fallback_start: dt.date,
    end_date: dt.date,
) -> CollectionResult:
    start_date = incremental_start_date(connection, symbol, fallback_start)
    candles = fetch_history(session, access_token, symbol, start_date, end_date)
    affected = store_candles(connection, candles)
    return CollectionResult(symbol, start_date.isoformat(), end_date.isoformat(), len(candles), affected)


def collect_all() -> list[CollectionResult]:
    token = get_access_token()
    start_date, end_date = configured_date_range()
    sleep_seconds = float(os.getenv("FYERS_SLEEP_SECONDS", "0.2"))
    results: list[CollectionResult] = []

    with connect_database() as connection, requests.Session() as session:
        for symbol in configured_symbols():
            result = collect_symbol(connection, session, token, symbol, start_date, end_date)
            LOGGER.info(
                "Collected %s candles for %s from %s to %s",
                result.fetched,
                result.symbol,
                result.requested_from,
                result.requested_to,
            )
            results.append(result)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return results


def main() -> None:
    results = collect_all()
    total = sum(result.fetched for result in results)
    LOGGER.info("FYERS data collection complete: %s symbols, %s candles", len(results), total)


if __name__ == "__main__":
    main()
