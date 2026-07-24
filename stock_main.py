"""Termux-friendly Upstox 1-minute traded-value scanner.

Run:
    export UPSTOX_ACCESS_TOKEN="..."
    export TELEGRAM_BOT_TOKEN="..."      # optional; STOCK_BOT_TOKEN also works
    export TELEGRAM_CHAT_ID="..."        # optional; STOCK_CHAT_ID also works
    python stock_main.py

Scanner rules implemented exactly:
- Universe is NSE F&O equity underlyings from the Upstox NSE instrument file.
- If that universe cannot be loaded, it automatically falls back to NIFTY 500.
- Fetches 1-minute intraday candles from Upstox Analytics/Historical Data API v3.
- Every 5 minutes scans all symbols.
- Traded value per candle is close * volume.
- Alert when 4 or 5 consecutive completed 1-minute candles have traded value >= ₹4 crore.
- No indicators, breakout logic, ranking, or sector filters.
- Telegram alert is sent only once per qualifying sequence.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

UPSTOX_INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday/{instrument_key}/minutes/1"
UPSTOX_NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY_500_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

SCAN_INTERVAL_SECONDS = int(os.getenv("STOCK_SCAN_INTERVAL_SECONDS", "300"))
REQUEST_TIMEOUT = int(os.getenv("STOCK_REQUEST_TIMEOUT", "20"))
REQUEST_SLEEP_SECONDS = float(os.getenv("STOCK_REQUEST_SLEEP_SECONDS", "0.12"))
TRADED_VALUE_THRESHOLD = float(os.getenv("STOCK_TRADED_VALUE_THRESHOLD", "40000000"))
MIN_SEQUENCE_LENGTH = 4
MAX_SEQUENCE_LENGTH = 5
STATE_FILE = Path(os.getenv("STOCK_STATE_FILE", ".stock_traded_value_alert_state.json"))
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=os.getenv("STOCK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("upstox_traded_value_scanner")
STOP_REQUESTED = False


@dataclass(frozen=True)
class Candle:
    start_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def traded_value(self) -> float:
        return self.close * self.volume

    @property
    def completed_at(self) -> datetime:
        return self.start_time + timedelta(minutes=1)


@dataclass(frozen=True)
class SymbolInstrument:
    symbol: str
    instrument_key: str


@dataclass(frozen=True)
class QualifyingSequence:
    symbol: str
    candles: tuple[Candle, ...]

    @property
    def alert_key(self) -> str:
        # A continuous run should alert only once, even if later scans see the
        # same run extended beyond the original 4 or 5 candles.
        first = self.candles[0].start_time.isoformat()
        return f"{self.symbol}:{first}"


def request_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOGGER.info("received signal %s; stopping after current work", signum)


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
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("could not read alert state %s: %s", STATE_FILE, exc)
        return set()
    return set(data.get("sent_alerts", []))


def save_state(sent_alerts: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"sent_alerts": sorted(sent_alerts)}, indent=2),
        encoding="utf-8",
    )


def auth_headers() -> dict[str, str]:
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN environment variable is required")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def fetch_upstox_nse_instruments(session: requests.Session) -> list[dict[str, Any]]:
    response = session.get(UPSTOX_NSE_INSTRUMENTS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = gzip.decompress(response.content).decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("unexpected Upstox NSE instruments JSON structure")
    return [item for item in data if isinstance(item, dict)]


def build_equity_map(instruments: list[dict[str, Any]]) -> dict[str, SymbolInstrument]:
    equities: dict[str, SymbolInstrument] = {}
    for item in instruments:
        if item.get("segment") != "NSE_EQ" or item.get("instrument_type") != "EQ":
            continue
        symbol = str(item.get("trading_symbol") or item.get("short_name") or "").upper().strip()
        key = str(item.get("instrument_key") or "").strip()
        if symbol and key:
            equities[symbol] = SymbolInstrument(symbol=symbol, instrument_key=key)
    return equities


def load_fo_universe(session: requests.Session) -> list[SymbolInstrument]:
    instruments = fetch_upstox_nse_instruments(session)
    equities = build_equity_map(instruments)
    underlying_symbols = {
        str(item.get("underlying_symbol") or "").upper().strip()
        for item in instruments
        if item.get("segment") == "NSE_FO" and str(item.get("underlying_type") or "").upper() == "EQUITY"
    }
    universe = [equities[symbol] for symbol in sorted(underlying_symbols) if symbol in equities]
    if not universe:
        raise ValueError("no NSE F&O equity underlyings mapped to NSE_EQ instruments")
    LOGGER.info("loaded %s NSE F&O stock underlyings", len(universe))
    return universe


def parse_nifty500_symbols(csv_text: str) -> list[str]:
    reader = csv.DictReader(csv_text.splitlines())
    symbols: list[str] = []
    for row in reader:
        symbol = (row.get("Symbol") or row.get("symbol") or "").upper().strip()
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def load_nifty500_universe(session: requests.Session) -> list[SymbolInstrument]:
    instruments = fetch_upstox_nse_instruments(session)
    equities = build_equity_map(instruments)
    response = session.get(NIFTY_500_CSV_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    symbols = parse_nifty500_symbols(response.text)
    universe = [equities[symbol] for symbol in symbols if symbol in equities]
    if not universe:
        raise ValueError("no NIFTY 500 symbols mapped to NSE_EQ instruments")
    LOGGER.info("loaded %s NIFTY 500 stocks", len(universe))
    return universe


def load_universe(session: requests.Session) -> list[SymbolInstrument]:
    try:
        return load_fo_universe(session)
    except Exception as exc:
        LOGGER.warning("NSE F&O universe load failed; falling back to NIFTY 500: %s", exc)
        return load_nifty500_universe(session)


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def parse_candles(payload: dict[str, Any]) -> list[Candle]:
    raw_candles = ((payload.get("data") or {}).get("candles") or [])
    candles: list[Candle] = []
    for raw in raw_candles:
        if not isinstance(raw, list) or len(raw) < 6:
            continue
        start_time = parse_time(raw[0])
        candle = Candle(
            start_time=start_time or datetime.fromtimestamp(0, tz=timezone.utc),
            open=as_float(raw[1]),
            high=as_float(raw[2]),
            low=as_float(raw[3]),
            close=as_float(raw[4]),
            volume=as_float(raw[5]),
        )
        if candle.start_time.year > 1970 and candle.close > 0 and candle.volume >= 0:
            candles.append(candle)
    return sorted(candles, key=lambda item: item.start_time)


def fetch_candles(session: requests.Session, instrument: SymbolInstrument) -> list[Candle]:
    url = UPSTOX_INTRADAY_URL.format(instrument_key=quote(instrument.instrument_key, safe=""))
    response = session.get(url, headers=auth_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return parse_candles(response.json())


def completed_candles(candles: list[Candle]) -> list[Candle]:
    now = datetime.now(timezone.utc)
    return [candle for candle in candles if candle.completed_at <= now]


def find_latest_qualifying_sequence(symbol: str, candles: list[Candle]) -> QualifyingSequence | None:
    qualified = [candle for candle in completed_candles(candles) if candle.traded_value >= TRADED_VALUE_THRESHOLD]
    if len(qualified) < MIN_SEQUENCE_LENGTH:
        return None

    runs: list[list[Candle]] = []
    current: list[Candle] = []
    for candle in qualified:
        if current and candle.start_time - current[-1].start_time != timedelta(minutes=1):
            runs.append(current)
            current = []
        current.append(candle)
    if current:
        runs.append(current)

    for run in reversed(runs):
        if len(run) >= MIN_SEQUENCE_LENGTH:
            selected = tuple(run[:MAX_SEQUENCE_LENGTH])
            return QualifyingSequence(symbol=symbol, candles=selected)
    return None


def format_rupees(value: float) -> str:
    return f"₹{value:,.0f}"


def format_alert(sequence: QualifyingSequence) -> str:
    first = sequence.candles[0].start_time.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    last = sequence.candles[-1].start_time.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    lines = [
        "Upstox volume scanner alert",
        f"Symbol: {sequence.symbol}",
        f"Condition: {len(sequence.candles)} consecutive completed 1-minute candles >= ₹4 crore traded value",
        f"Window: {first} to {last}",
        "Candles:",
    ]
    for candle in sequence.candles:
        minute = candle.start_time.astimezone(IST).strftime("%H:%M")
        lines.append(
            f"- {minute}: close {candle.close:g}, volume {candle.volume:g}, traded value {format_rupees(candle.traded_value)}"
        )
    return "\n".join(lines)


def send_telegram(session: requests.Session, message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("STOCK_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("STOCK_CHAT_ID")
    if not token or not chat_id:
        LOGGER.info("Telegram env vars missing; qualifying alert not sent")
        return False
    response = session.post(
        TELEGRAM_URL.format(token=token),
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return True


def scan_once(session: requests.Session, universe: list[SymbolInstrument], sent_alerts: set[str]) -> None:
    matches = 0
    alerts_sent = 0
    for instrument in universe:
        if STOP_REQUESTED:
            break
        try:
            candles = fetch_candles(session, instrument)
            sequence = find_latest_qualifying_sequence(instrument.symbol, candles)
        except requests.RequestException as exc:
            LOGGER.warning("%s candle fetch failed: %s", instrument.symbol, exc)
            continue
        except Exception as exc:
            LOGGER.warning("%s scan failed: %s", instrument.symbol, exc)
            continue

        if sequence:
            matches += 1
            if sequence.alert_key in sent_alerts:
                LOGGER.info("duplicate sequence skipped: %s", sequence.alert_key)
            elif send_telegram(session, format_alert(sequence)):
                sent_alerts.add(sequence.alert_key)
                save_state(sent_alerts)
                alerts_sent += 1
                LOGGER.info("alert sent: %s", sequence.alert_key)

        if REQUEST_SLEEP_SECONDS > 0:
            time.sleep(REQUEST_SLEEP_SECONDS)

    LOGGER.info("scan complete: matches=%s alerts_sent=%s", matches, alerts_sent)


def sleep_until_next_scan(started_at: float) -> None:
    elapsed = time.monotonic() - started_at
    remaining = max(0.0, SCAN_INTERVAL_SECONDS - elapsed)
    LOGGER.info("sleeping %.1f seconds before next scan", remaining)
    end_time = time.monotonic() + remaining
    while not STOP_REQUESTED and time.monotonic() < end_time:
        time.sleep(min(1.0, end_time - time.monotonic()))


def run() -> None:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    session = requests.Session()
    universe = load_universe(session)
    sent_alerts = load_state()
    LOGGER.info("scanner started with %s symbols; scan interval=%ss", len(universe), SCAN_INTERVAL_SECONDS)

    while not STOP_REQUESTED:
        started_at = time.monotonic()
        scan_once(session, universe, sent_alerts)
        if STOP_REQUESTED:
            break
        sleep_until_next_scan(started_at)

    LOGGER.info("scanner stopped")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        LOGGER.error("fatal scanner error: %s", exc)
        sys.exit(1)
