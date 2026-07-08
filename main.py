"""CoinDCX USDT volume breakout Telegram scanner.

Scans CoinDCX USDT markets on the 15-minute timeframe, looking for a quiet
("dead volume") window followed by a relative volume spike and a price
breakout or breakdown on the latest closed candle. Alerts are sent to Telegram
and de-duplicated per pair, direction, and candle close time.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
COINDCX_CANDLES_URL = "https://public.coindcx.com/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

INTERVAL = "15m"
CANDLE_LIMIT = 80
REQUEST_TIMEOUT = 20
STATE_FILE = Path(os.getenv("STATE_FILE", ".alert_state.json"))

DEAD_VOLUME_LOOKBACK = int(os.getenv("DEAD_VOLUME_LOOKBACK", "8"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "12"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "1000"))
SCAN_SLEEP_SECONDS = float(os.getenv("SCAN_SLEEP_SECONDS", "0.25"))
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "0"))

DEAD_VOLUME_RATIO = float(os.getenv("DEAD_VOLUME_RATIO", "0.72"))
SPIKE_MULTIPLIER = float(os.getenv("SPIKE_MULTIPLIER", "2.2"))
MAX_QUIET_RANGE_PCT = float(os.getenv("MAX_QUIET_RANGE_PCT", "3.0"))
MIN_BREAK_PCT = float(os.getenv("MIN_BREAK_PCT", "0.18"))
ENTRY_PROXIMITY_PCT = float(os.getenv("ENTRY_PROXIMITY_PCT", "0.12"))
MIN_BODY_PCT = float(os.getenv("MIN_BODY_PCT", "0.35"))
MIN_CLOSE_LOCATION = float(os.getenv("MIN_CLOSE_LOCATION", "0.62"))
MAX_EXTENSION_FROM_RANGE_PCT = float(os.getenv("MAX_EXTENSION_FROM_RANGE_PCT", "9.0"))
DAILY_TOP_PER_DIRECTION = int(os.getenv("DAILY_TOP_PER_DIRECTION", "2"))

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
    quiet_range_pct: float
    break_pct: float
    confirmation_candle: Candle

    @property
    def alert_key(self) -> str:
        return f"{self.pair}:{self.direction}:{self.confirmation_candle.timestamp}"

    @property
    def daily_direction(self) -> str:
        return "UP" if self.direction == "BREAKOUT" else "DOWN"

    @property
    def quiet_quality(self) -> float:
        if MAX_QUIET_RANGE_PCT <= 0:
            return 0.0
        return max(0.0, (MAX_QUIET_RANGE_PCT - self.quiet_range_pct) / MAX_QUIET_RANGE_PCT)

    @property
    def rank_score(self) -> float:
        quote_volume_score = math.log10(max(self.candle.quote_volume, 1.0))
        return (
            self.spike_ratio * 35
            + self.break_pct * 25
            + quote_volume_score * 10
            + self.quiet_quality * 30
        )

    @property
    def entry_timing(self) -> str:
        if self.break_pct >= MIN_BREAK_PCT:
            return "close breakout"
        return "early range-edge ignition"


@dataclass(frozen=True)
class SignalEvaluation:
    signal: Signal | None
    rejection_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.signal is not None


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
        if not market.get("coindcx_name") or not market.get("pair"):
            continue
        if market.get("base_currency_short_name") != "USDT":
            continue
        if market.get("status", "active").lower() not in {"active", "online"}:
            continue
        pairs.append(str(market["pair"]))

    unique_pairs = sorted(set(pairs))
    if MAX_PAIRS > 0:
        return unique_pairs[:MAX_PAIRS]
    return unique_pairs


def normalize_candle(raw: dict[str, Any] | list[Any]) -> Candle:
    if isinstance(raw, list):
        timestamp, open_price, high, low, close, volume = (raw + [0] * 6)[:6]
        return Candle(
            open=as_float(open_price),
            high=as_float(high),
            low=as_float(low),
            close=as_float(close),
            volume=as_float(volume),
            timestamp=int(as_float(timestamp)),
        )

    return Candle(
        open=as_float(raw.get("open")),
        high=as_float(raw.get("high")),
        low=as_float(raw.get("low")),
        close=as_float(raw.get("close")),
        volume=as_float(raw.get("volume")),
        timestamp=int(as_float(raw.get("time") or raw.get("timestamp"))),
    )


def parse_candle_response(payload: Any) -> list[Candle]:
    if isinstance(payload, dict):
        raw_candles = payload.get("data") or payload.get("candles") or []
    elif isinstance(payload, list):
        raw_candles = payload
    else:
        raw_candles = []

    candles = [normalize_candle(item) for item in raw_candles if isinstance(item, (dict, list))]
    valid_candles = (candle for candle in candles if candle.timestamp)
    return sorted(valid_candles, key=lambda item: item.timestamp)


 codex/fix-coindcx-candle-fetching-issue-la3ynk
def get_candles(session: requests.Session, pair: str, *, log_response: bool = False) -> list[Candle]:

def get_candles(session: requests.Session, pair: str) -> list[Candle]:
 main
    response = session.get(
        COINDCX_CANDLES_URL,
        params={"pair": pair, "interval": INTERVAL, "limit": CANDLE_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    if log_response:
        LOGGER.info(
            "Sample CoinDCX candles response for %s: status=%s body=%s",
            pair,
            response.status_code,
            response.text,
        )
    response.raise_for_status()
    return parse_candle_response(response.json())


def average(values: Iterable[float]) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else 0.0


def detect_signal(pair: str, candles: list[Candle]) -> Signal | None:
    return evaluate_signal(pair, candles).signal


def evaluate_signal(pair: str, candles: list[Candle]) -> SignalEvaluation:
    minimum_needed = max(DEAD_VOLUME_LOOKBACK, BREAKOUT_LOOKBACK) + BREAKOUT_LOOKBACK + 1
    if len(candles) < minimum_needed:
        return SignalEvaluation(None, "too_few_candles")

    closed_candles = candles[:-1]
    latest_closed_index = len(closed_candles) - 1
    return build_signal(pair, closed_candles, latest_closed_index)


def percent_change(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return ((current - reference) / reference) * 100


def candle_range(candle: Candle) -> float:
    return max(candle.high - candle.low, 0.0)


def body_ratio(candle: Candle) -> float:
    price_range = candle_range(candle)
    if price_range <= 0:
        return 0.0
    return abs(candle.close - candle.open) / price_range


def close_location(candle: Candle) -> float:
    price_range = candle_range(candle)
    if price_range <= 0:
        return 0.5
    return (candle.close - candle.low) / price_range


def is_real_breakout_candle(candle: Candle) -> bool:
    return candle.close > candle.open and body_ratio(candle) >= MIN_BODY_PCT


def is_real_breakdown_candle(candle: Candle) -> bool:
    return candle.close < candle.open and body_ratio(candle) >= MIN_BODY_PCT


def build_signal(pair: str, closed_candles: list[Candle], trigger_index: int) -> SignalEvaluation:
    spike_candle = closed_candles[trigger_index]
    dead_start = trigger_index - DEAD_VOLUME_LOOKBACK
    breakout_start = trigger_index - BREAKOUT_LOOKBACK
    if dead_start < 0 or breakout_start < 0:
        return SignalEvaluation(None, "insufficient_history_window")

    dead_window = closed_candles[dead_start:trigger_index]
    breakout_window = closed_candles[breakout_start:trigger_index]
    history = closed_candles[:dead_start]
    if len(dead_window) < DEAD_VOLUME_LOOKBACK or len(breakout_window) < BREAKOUT_LOOKBACK:
        return SignalEvaluation(None, "insufficient_history_window")

    dead_average = average(candle.volume for candle in dead_window)
    previous_average = average(candle.volume for candle in history[-BREAKOUT_LOOKBACK:])
    if previous_average <= 0 or dead_average <= 0:
        return SignalEvaluation(None, "zero_volume_average")

    quiet_high = max(candle.high for candle in breakout_window)
    quiet_low = min(candle.low for candle in breakout_window)
    quiet_mid = (quiet_high + quiet_low) / 2
    quiet_range_pct = percent_change(quiet_high, quiet_low)
    if quiet_mid <= 0 or quiet_range_pct > MAX_QUIET_RANGE_PCT:
        return SignalEvaluation(None, "quiet_range_too_wide")

    is_dead_volume = dead_average <= previous_average * DEAD_VOLUME_RATIO
    is_spike = spike_candle.volume >= dead_average * SPIKE_MULTIPLIER
    has_minimum_liquidity = spike_candle.quote_volume >= MIN_QUOTE_VOLUME
    if not (is_dead_volume and is_spike and has_minimum_liquidity):
        if not is_dead_volume:
            return SignalEvaluation(None, "dead_volume_not_low_enough")
        if not is_spike:
            return SignalEvaluation(None, "volume_spike_too_small")
        return SignalEvaluation(None, "quote_volume_too_low")

    upper_break_pct = percent_change(spike_candle.close, quiet_high)
    lower_break_pct = percent_change(quiet_low, spike_candle.close)
    max_extension = MAX_EXTENSION_FROM_RANGE_PCT
    upper_range_edge_ignition = (
        spike_candle.high >= quiet_high
        and percent_change(spike_candle.close, quiet_high) >= -ENTRY_PROXIMITY_PCT
    )
    lower_range_edge_ignition = (
        spike_candle.low <= quiet_low
        and percent_change(quiet_low, spike_candle.close) >= -ENTRY_PROXIMITY_PCT
    )

    if (
        (upper_break_pct >= MIN_BREAK_PCT or upper_range_edge_ignition)
        and upper_break_pct <= max_extension
        and is_real_breakout_candle(spike_candle)
        and close_location(spike_candle) >= MIN_CLOSE_LOCATION
    ):
        return SignalEvaluation(Signal(
            pair=pair,
            direction="BREAKOUT",
            candle=spike_candle,
            dead_average=dead_average,
            previous_average=previous_average,
            breakout_level=quiet_high,
            spike_ratio=spike_candle.volume / dead_average,
            quiet_range_pct=quiet_range_pct,
            break_pct=max(upper_break_pct, 0.0),
            confirmation_candle=spike_candle,
        ))
    if (
        (lower_break_pct >= MIN_BREAK_PCT or lower_range_edge_ignition)
        and lower_break_pct <= max_extension
        and is_real_breakdown_candle(spike_candle)
        and close_location(spike_candle) <= (1 - MIN_CLOSE_LOCATION)
    ):
        return SignalEvaluation(Signal(
            pair=pair,
            direction="BREAKDOWN",
            candle=spike_candle,
            dead_average=dead_average,
            previous_average=previous_average,
            breakout_level=quiet_low,
            spike_ratio=spike_candle.volume / dead_average,
            quiet_range_pct=quiet_range_pct,
            break_pct=max(lower_break_pct, 0.0),
            confirmation_candle=spike_candle,
        ))

    if not (upper_break_pct >= MIN_BREAK_PCT or upper_range_edge_ignition or lower_break_pct >= MIN_BREAK_PCT or lower_range_edge_ignition):
        return SignalEvaluation(None, "no_breakout_or_range_edge_touch")
    if (upper_break_pct >= MIN_BREAK_PCT or upper_range_edge_ignition) and upper_break_pct > max_extension:
        return SignalEvaluation(None, "breakout_overextended")
    if (lower_break_pct >= MIN_BREAK_PCT or lower_range_edge_ignition) and lower_break_pct > max_extension:
        return SignalEvaluation(None, "breakdown_overextended")
    if upper_break_pct >= MIN_BREAK_PCT or upper_range_edge_ignition:
        if not is_real_breakout_candle(spike_candle):
            return SignalEvaluation(None, "breakout_body_too_weak")
        return SignalEvaluation(None, "breakout_close_not_strong_enough")
    if not is_real_breakdown_candle(spike_candle):
        return SignalEvaluation(None, "breakdown_body_too_weak")
    return SignalEvaluation(None, "breakdown_close_not_strong_enough")


def current_day_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def daily_sent_key(day_key: str, direction: str, rank: int) -> str:
    return f"daily:{day_key}:{direction}:{rank}"


def sent_daily_count(sent_alerts: set[str], day_key: str, direction: str) -> int:
    prefix = f"daily:{day_key}:{direction}:"
    return sum(1 for item in sent_alerts if item.startswith(prefix))


def rank_signals(signals: list[Signal]) -> list[Signal]:
    return sorted(
        signals,
        key=lambda signal: (
            signal.rank_score,
            signal.spike_ratio,
            signal.break_pct,
            signal.candle.quote_volume,
            -signal.quiet_range_pct,
        ),
        reverse=True,
    )


def select_daily_top_signals(signals: list[Signal], sent_alerts: set[str], day_key: str) -> list[tuple[int, Signal]]:
    selected: list[tuple[int, Signal]] = []
    for direction in ("UP", "DOWN"):
        already_sent = sent_daily_count(sent_alerts, day_key, direction)
        remaining_slots = max(DAILY_TOP_PER_DIRECTION - already_sent, 0)
        if remaining_slots <= 0:
            continue

        direction_signals = [
            signal
            for signal in signals
            if signal.daily_direction == direction and signal.alert_key not in sent_alerts
        ]
        for offset, signal in enumerate(rank_signals(direction_signals)[:remaining_slots], start=1):
            selected.append((already_sent + offset, signal))
    return selected


def format_alert(signal: Signal, rank: int, day_key: str) -> str:
    emoji = "🚀" if signal.direction == "BREAKOUT" else "🔻"
    return (
        f"{emoji} CoinDCX 15m daily #{rank} {signal.daily_direction} signal ({signal.direction})\n"
        f"Ranking day: {day_key} UTC\n"
        f"Pair: {signal.pair}\n"
        f"Close: {signal.candle.close:g}\n"
        f"Level: {signal.breakout_level:g}\n"
        f"Volume spike: {signal.spike_ratio:.2f}x dead-volume average\n"
        f"Quiet range: {signal.quiet_range_pct:.2f}%\n"
        f"Break strength: {signal.break_pct:.2f}%\n"
        f"Quiet quality: {signal.quiet_quality:.2%} (tighter quiet range ranks higher)\n"
        f"Rank score: {signal.rank_score:.2f}\n"
        f"Entry timing: {signal.entry_timing}\n"
        f"Ranking formula: spike_ratio*35 + break_pct*25 + log10(quote_volume)*10 + quiet_quality*30\n"
        f"Dead avg volume: {signal.dead_average:.4f}\n"
        f"Previous avg volume: {signal.previous_average:.4f}\n"
        f"Quote volume: {signal.candle.quote_volume:.2f} USDT\n"
        f"Trigger candle time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(signal.candle.timestamp / 1000))}\n"
        f"Alert basis: latest closed 15m candle; no delayed 5-candle confirmation\n"
        f"Alert candle time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(signal.confirmation_candle.timestamp / 1000))}"
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
        valid_signals: list[Signal] = []
        rejection_counts: Counter[str] = Counter()
        sample_candle_counts: dict[str, int] = {}
        sample_response_logged = False
        for pair in pairs:
            try:
                log_response = not sample_response_logged
                candles = get_candles(session, pair, log_response=log_response)
                sample_response_logged = sample_response_logged or log_response
                if len(sample_candle_counts) < 3:
                    sample_candle_counts[pair] = len(candles)
                evaluation = evaluate_signal(pair, candles)
            except requests.RequestException as exc:
                LOGGER.warning("Skipping %s after API error: %s", pair, exc)
                rejection_counts["api_error"] += 1
                continue

            if evaluation.signal:
                valid_signals.append(evaluation.signal)
            elif evaluation.rejection_reason:
                rejection_counts[evaluation.rejection_reason] += 1
            time.sleep(SCAN_SLEEP_SECONDS)

        if sample_candle_counts:
            LOGGER.debug("Sample candle counts for first 3 pairs: %s", sample_candle_counts)

        LOGGER.info("Detected %s valid signal candidate(s)", len(valid_signals))
        if rejection_counts:
            LOGGER.info("Signal rejection counts: %s", dict(rejection_counts.most_common()))

        day_key = current_day_key()
        selected_signals = select_daily_top_signals(valid_signals, sent_alerts, day_key)
        LOGGER.info("Selected %s alert(s) after daily ranking and duplicate filters", len(selected_signals))
        for rank, signal in selected_signals:
            send_telegram_alert(session, bot_token, chat_id, format_alert(signal, rank, day_key))
            sent_alerts.add(signal.alert_key)
            sent_alerts.add(daily_sent_key(day_key, signal.daily_direction, rank))
            alerts_sent += 1
            LOGGER.info("Sent daily #%s %s alert for %s", rank, signal.daily_direction, signal.pair)

    save_state(sent_alerts)
    LOGGER.info("Scan complete; sent %s alert(s)", alerts_sent)
    return alerts_sent


if __name__ == "__main__":
    run_scan()
