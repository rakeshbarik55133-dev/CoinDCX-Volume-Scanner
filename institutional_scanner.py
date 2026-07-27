"""CoinDCX institutional pullback scanner for 15-minute spot candles."""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

TIMEFRAME = "15m"
TIMEFRAME_MS = 15 * 60 * 1000
CANDLE_LIMIT = 120
SCAN_INTERVAL_SECONDS = float(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
PAIR_REQUEST_DELAY_SECONDS = float(os.getenv("PAIR_REQUEST_DELAY_SECONDS", "0.10"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "2"))

VOLUME_LOOKBACK = 20
RANGE_LOOKBACK = 20
IMPULSE_VOLUME_MULTIPLIER = 1.8
IMPULSE_RANGE_MULTIPLIER = 1.5
MIN_IMPULSE_BODY_RATIO = 0.65
MIN_PULLBACK_CANDLES = 2
MAX_PULLBACK_CANDLES = 8
MAX_RETRACE_FRACTION = 0.65
MAX_PULLBACK_TO_IMPULSE_VOLUME = 0.75
MIN_CONTRACTING_VOLUME_FRACTION = 0.60
BREAKOUT_BUFFER_FRACTION = 0.0005
ALERT_RETENTION_DAYS = 30

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.getenv("INSTITUTIONAL_SCANNER_STATE", ".institutional_scanner_state.json"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class JsonFormatter(logging.Formatter):
    """Render log records as one-line JSON for ingestion by log platforms."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "pair", "side", "attempt", "pair_count", "signal_count"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, separators=(",", ":"), default=str)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
LOGGER = logging.getLogger("institutional_scanner")
LOGGER.handlers.clear()
LOGGER.addHandler(handler)
LOGGER.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
LOGGER.propagate = False


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class Signal:
    pair: str
    display_pair: str
    side: Literal["BUY", "SELL"]
    impulse: Candle
    pullback: tuple[Candle, ...]
    breakout: Candle
    breakout_level: float
    impulse_volume_ratio: float

    @property
    def key(self) -> str:
        return f"{self.pair}:{self.side}:{self.breakout.timestamp}"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _timestamp(value: Any) -> int:
    timestamp = int(_number(value))
    return timestamp * 1000 if 1_000_000_000 <= timestamp < 10_000_000_000 else timestamp


def _average(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def parse_candle(raw: Any) -> Candle | None:
    if isinstance(raw, dict):
        values = (
            raw.get("time", raw.get("timestamp", raw.get("t"))),
            raw.get("open", raw.get("o")),
            raw.get("high", raw.get("h")),
            raw.get("low", raw.get("l")),
            raw.get("close", raw.get("c")),
            raw.get("volume", raw.get("v")),
        )
    elif isinstance(raw, list) and len(raw) >= 6:
        values = raw[:6]
    else:
        return None

    candle = Candle(_timestamp(values[0]), *(_number(value) for value in values[1:]))
    if (
        candle.timestamp <= 0
        or candle.open <= 0
        or candle.low <= 0
        or candle.high < candle.low
        or not candle.low <= candle.open <= candle.high
        or not candle.low <= candle.close <= candle.high
        or candle.volume < 0
    ):
        return None
    return candle


def parse_candles(payload: Any) -> list[Candle]:
    if isinstance(payload, dict):
        raw_candles = payload.get("data", payload.get("candles", []))
    else:
        raw_candles = payload
    if not isinstance(raw_candles, list):
        return []
    unique: dict[int, Candle] = {}
    for raw in raw_candles:
        candle = parse_candle(raw)
        if candle is not None:
            unique[candle.timestamp] = candle
    return sorted(unique.values(), key=lambda item: item.timestamp)


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    operation: str,
    **kwargs: Any,
) -> Any:
    """Make an HTTP request with bounded exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            LOGGER.warning(
                "%s failed: %s",
                operation,
                exc,
                extra={"event": "network_retry", "attempt": attempt},
            )
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                time.sleep(delay)
    raise RuntimeError(f"{operation} failed after {MAX_RETRIES} attempts") from last_error


def active_usdt_pairs(session: requests.Session) -> dict[str, str]:
    """Fetch CoinDCX metadata anew and return candle pair -> display symbol."""
    payload = request_json(session, "GET", MARKETS_URL, operation="fetch markets")
    if not isinstance(payload, list):
        raise RuntimeError("CoinDCX markets response was not a list")

    pairs: dict[str, str] = {}
    for market in payload:
        if not isinstance(market, dict):
            continue
        status = str(market.get("status", "")).lower()
        target = str(market.get("target_currency_short_name", "")).upper()
        ecode = str(market.get("ecode", "")).upper()
        if status not in {"active", "online"} or target != "USDT" or ecode != "B":
            continue
        candle_pair = str(market.get("pair") or "").upper().strip()
        if not candle_pair:
            continue
        display = str(market.get("coindcx_name") or market.get("symbol") or candle_pair)
        pairs[candle_pair] = display.upper().replace("_", "").removeprefix("B-")
    return dict(sorted(pairs.items()))


def closed_candles(session: requests.Session, pair: str) -> list[Candle]:
    payload = request_json(
        session,
        "GET",
        CANDLES_URL,
        operation=f"fetch candles for {pair}",
        params={"pair": pair, "interval": TIMEFRAME, "limit": CANDLE_LIMIT},
    )
    now_ms = int(time.time() * 1000)
    return [candle for candle in parse_candles(payload) if candle.timestamp + TIMEFRAME_MS <= now_ms]


def _is_impulse(candles: list[Candle], index: int, side: str) -> tuple[bool, float]:
    if index < max(VOLUME_LOOKBACK, RANGE_LOOKBACK):
        return False, 0.0
    candle = candles[index]
    average_volume = _average(c.volume for c in candles[index - VOLUME_LOOKBACK : index])
    average_range = _average(c.range for c in candles[index - RANGE_LOOKBACK : index])
    directional = candle.close > candle.open if side == "BUY" else candle.close < candle.open
    volume_ratio = candle.volume / average_volume if average_volume else 0.0
    return (
        directional
        and candle.range > 0
        and candle.body / candle.range >= MIN_IMPULSE_BODY_RATIO
        and candle.range >= average_range * IMPULSE_RANGE_MULTIPLIER
        and volume_ratio >= IMPULSE_VOLUME_MULTIPLIER,
        volume_ratio,
    )


def _healthy_pullback(impulse: Candle, pullback: list[Candle], side: str) -> bool:
    if not MIN_PULLBACK_CANDLES <= len(pullback) <= MAX_PULLBACK_CANDLES:
        return False
    volumes = [candle.volume for candle in pullback]
    contracting_steps = sum(later <= earlier for earlier, later in zip(volumes, volumes[1:]))
    required_steps = max(1, int((len(volumes) - 1) * MIN_CONTRACTING_VOLUME_FRACTION))
    volume_contracts = (
        _average(volumes) <= impulse.volume * MAX_PULLBACK_TO_IMPULSE_VOLUME
        and contracting_steps >= required_steps
    )
    if side == "BUY":
        retracement = impulse.high - min(candle.low for candle in pullback)
        structure_holds = min(candle.low for candle in pullback) > impulse.low
        counter_move_exists = any(candle.close < candle.open for candle in pullback)
    else:
        retracement = max(candle.high for candle in pullback) - impulse.low
        structure_holds = max(candle.high for candle in pullback) < impulse.high
        counter_move_exists = any(candle.close > candle.open for candle in pullback)
    return (
        volume_contracts
        and structure_holds
        and counter_move_exists
        and retracement <= impulse.range * MAX_RETRACE_FRACTION
    )


def detect_signal(pair: str, display_pair: str, candles: list[Candle]) -> Signal | None:
    """Evaluate only the latest closed candle as the first continuation candle."""
    if len(candles) < max(VOLUME_LOOKBACK, RANGE_LOOKBACK) + MIN_PULLBACK_CANDLES + 2:
        return None
    breakout = candles[-1]
    for pullback_length in range(MIN_PULLBACK_CANDLES, MAX_PULLBACK_CANDLES + 1):
        impulse_index = len(candles) - pullback_length - 2
        if impulse_index < 0:
            break
        impulse = candles[impulse_index]
        pullback = candles[impulse_index + 1 : -1]
        for side in ("BUY", "SELL"):
            is_impulse, volume_ratio = _is_impulse(candles, impulse_index, side)
            if not is_impulse or not _healthy_pullback(impulse, pullback, side):
                continue
            if side == "BUY":
                level = max(candle.high for candle in pullback)
                breaks = breakout.close > level * (1 + BREAKOUT_BUFFER_FRACTION)
                continuation = breakout.close > breakout.open
            else:
                level = min(candle.low for candle in pullback)
                breaks = breakout.close < level * (1 - BREAKOUT_BUFFER_FRACTION)
                continuation = breakout.close < breakout.open
            if breaks and continuation:
                return Signal(
                    pair, display_pair, side, impulse, tuple(pullback), breakout, level, volume_ratio
                )
    return None


def load_alerts() -> dict[str, int]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("state must be an object")
        return {str(key): int(value) for key, value in payload.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        LOGGER.error("Unable to read alert state: %s", exc, extra={"event": "state_read_failed"})
        return {}


def save_alerts(alerts: dict[str, int]) -> None:
    cutoff = int(time.time() * 1000) - ALERT_RETENTION_DAYS * 86_400_000
    retained = {key: timestamp for key, timestamp in alerts.items() if timestamp >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(retained, sort_keys=True, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def send_telegram(session: requests.Session, message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured")
    payload = request_json(
        session,
        "POST",
        TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN),
        operation="send Telegram message",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
    )
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def signal_message(found: Signal) -> str:
    icon = "🟢" if found.side == "BUY" else "🔴"
    return (
        f"{icon} Institutional Pullback {found.side}\n"
        f"Pair: {found.display_pair}\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"Breakout close: {found.breakout.close:.10g}\n"
        f"Breakout level: {found.breakout_level:.10g}\n"
        f"Pullback candles: {len(found.pullback)}\n"
        f"Impulse volume: {found.impulse_volume_ratio:.2f}x average\n"
        f"Candle time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(found.breakout.timestamp / 1000))}"
    )


STOP_REQUESTED = False


def _request_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOGGER.info("Shutdown requested", extra={"event": "shutdown_requested"})


def run_scan(session: requests.Session, sent_alerts: dict[str, int]) -> int:
    pairs = active_usdt_pairs(session)  # Deliberately refreshed on every scan.
    LOGGER.info("Active CoinDCX pairs loaded", extra={"event": "pairs_refreshed", "pair_count": len(pairs)})
    signal_count = 0
    for pair, display_pair in pairs.items():
        if STOP_REQUESTED:
            break
        try:
            found = detect_signal(pair, display_pair, closed_candles(session, pair))
            if found is not None and found.key not in sent_alerts:
                send_telegram(session, signal_message(found))
                sent_alerts[found.key] = found.breakout.timestamp
                save_alerts(sent_alerts)
                signal_count += 1
                LOGGER.info(
                    "Signal alert sent",
                    extra={"event": "alert_sent", "pair": pair, "side": found.side},
                )
        except Exception:
            LOGGER.exception("Pair scan failed", extra={"event": "pair_scan_failed", "pair": pair})
        if PAIR_REQUEST_DELAY_SECONDS > 0:
            time.sleep(PAIR_REQUEST_DELAY_SECONDS)
    LOGGER.info("Scan completed", extra={"event": "scan_complete", "signal_count": signal_count})
    return signal_count


def main() -> None:
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    session = requests.Session()
    session.headers.update({"User-Agent": "CoinDCX-Institutional-Pullback-Scanner/1.0"})
    sent_alerts = load_alerts()

    startup_message = (
        "🚀 Institutional Pullback Scanner started\n"
        "Market: CoinDCX USDT spot pairs\n"
        f"Timeframe: {TIMEFRAME}"
    )
    while not STOP_REQUESTED:
        try:
            send_telegram(session, startup_message)
            break
        except RuntimeError as exc:
            if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
                raise
            LOGGER.error(
                "Startup notification failed; retrying: %s",
                exc,
                extra={"event": "startup_retry"},
            )
            time.sleep(RETRY_BASE_SECONDS)
    if STOP_REQUESTED:
        session.close()
        return
    LOGGER.info("Scanner started", extra={"event": "startup"})

    while not STOP_REQUESTED:
        started = time.monotonic()
        try:
            run_scan(session, sent_alerts)
        except Exception:
            LOGGER.exception("Scan failed", extra={"event": "scan_failed"})
        remaining = max(0.0, SCAN_INTERVAL_SECONDS - (time.monotonic() - started))
        deadline = time.monotonic() + remaining
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    session.close()
    LOGGER.info("Scanner stopped", extra={"event": "shutdown"})


if __name__ == "__main__":
    main()
