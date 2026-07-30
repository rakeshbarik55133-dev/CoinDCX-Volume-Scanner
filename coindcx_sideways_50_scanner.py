#!/usr/bin/env python3
"""CoinDCX 50+ candle sideways breakout/breakdown Telegram scanner."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

import requests

API_BASE = "https://api.coindcx.com"
PUBLIC_BASE = "https://public.coindcx.com"
MARKETS_DETAILS_URL = f"{API_BASE}/exchange/v1/markets_details"
TICKER_URL = f"{API_BASE}/exchange/ticker"
CANDLES_URL = f"{PUBLIC_BASE}/market_data/candles"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

TIMEFRAME = os.getenv("SIDEWAYS50_TIMEFRAME", "15m")
MIN_SIDEWAYS_CANDLES = int(os.getenv("SIDEWAYS50_MIN_CANDLES", "50"))
CANDLE_LIMIT = int(os.getenv("SIDEWAYS50_CANDLE_LIMIT", "300"))
MIN_INSIDE_CLOSE_RATIO = float(os.getenv("SIDEWAYS50_INSIDE_CLOSE_RATIO", "0.90"))
MAX_RANGE_PCT = float(os.getenv("SIDEWAYS50_MAX_RANGE_PCT", "0.08"))
MIN_VOLUME_MULTIPLE = float(os.getenv("SIDEWAYS50_MIN_VOLUME_MULTIPLE", "2.5"))
MIN_TRIGGER_QUOTE_VOLUME = float(os.getenv("SIDEWAYS50_MIN_TRIGGER_QUOTE_VOLUME", "10000"))
BREAKOUT_BUFFER_PCT = float(os.getenv("SIDEWAYS50_BREAKOUT_BUFFER_PCT", "0.001"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SIDEWAYS50_SCAN_INTERVAL_SECONDS", "3600"))
PAIR_DELAY_SECONDS = float(os.getenv("SIDEWAYS50_PAIR_DELAY_SECONDS", "0.08"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("SIDEWAYS50_REQUEST_TIMEOUT_SECONDS", "20"))
STATE_FILE = Path(os.getenv("SIDEWAYS50_STATE_FILE", ".sideways50_alert_state.json"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("sideways50")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 CoinDCX-Sideways50-Scanner/1.0"})
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def quote_volume(self) -> float:
        return self.volume * self.close


def request_json(url: str, params: dict[str, Any] | None = None) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Request failed: {url}: {last_error}")


def load_state() -> set[str]:
    try:
        raw = json.loads(STATE_FILE.read_text())
        if isinstance(raw, list):
            return {str(x) for x in raw}
    except (OSError, ValueError):
        pass
    return set()


def save_state(state: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(state)[-5000:], indent=2))


def active_usdt_pairs() -> list[dict[str, str]]:
    rows = request_json(MARKETS_DETAILS_URL)
    found: dict[str, dict[str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("status", "active")).lower() not in {"active", "online"}:
            continue
        if str(row.get("ecode", "")).upper() != "B":
            continue
        base = str(row.get("base_currency_short_name") or "").upper()
        target = str(row.get("target_currency_short_name") or "").upper()
        market = str(row.get("coindcx_name") or row.get("symbol") or "").upper()
        pair = str(row.get("pair") or "")
        if base != "USDT" or not target or not pair:
            continue
        found[market] = {"market": market, "pair": pair, "name": f"{target}/USDT"}
    return list(found.values())


def ticker_map() -> dict[str, dict[str, Any]]:
    rows = request_json(TICKER_URL)
    return {
        str(row.get("market") or "").upper(): row
        for row in rows if isinstance(rows, list) and isinstance(row, dict)
    }


def fetch_closed_candles(pair: str) -> list[Candle]:
    rows = request_json(CANDLES_URL, {"pair": pair, "interval": TIMEFRAME, "limit": CANDLE_LIMIT})
    candles: list[Candle] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            candles.append(Candle(
                timestamp=int(row["time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c.timestamp)
    interval_ms = 15 * 60 * 1000
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c.timestamp + interval_ms <= now_ms]


def human_money(value: float) -> str:
    value = max(0.0, value)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def human_price(value: float) -> str:
    if value >= 1:
        return f"${value:,.6f}".rstrip("0").rstrip(".")
    return f"${value:.10f}".rstrip("0").rstrip(".")


def find_setup(candles: list[Candle]) -> dict[str, Any] | None:
    if len(candles) < MIN_SIDEWAYS_CANDLES + 1:
        return None

    trigger = candles[-1]
    history = candles[:-1]
    best: dict[str, Any] | None = None
    max_len = min(len(history), CANDLE_LIMIT - 1)

    for base_len in range(MIN_SIDEWAYS_CANDLES, max_len + 1):
        base = history[-base_len:]
        base_high = max(c.high for c in base)
        base_low = min(c.low for c in base)
        midpoint = (base_high + base_low) / 2
        if midpoint <= 0:
            continue
        range_pct = (base_high - base_low) / midpoint
        if range_pct > MAX_RANGE_PCT:
            continue

        inside_closes = sum(base_low <= c.close <= base_high for c in base)
        if inside_closes / base_len < MIN_INSIDE_CLOSE_RATIO:
            continue

        average_volume = mean(c.volume for c in base)
        if average_volume <= 0:
            continue
        volume_multiple = trigger.volume / average_volume
        if volume_multiple < MIN_VOLUME_MULTIPLE:
            continue
        if trigger.quote_volume < MIN_TRIGGER_QUOTE_VOLUME:
            continue

        buy_level = base_high * (1 + BREAKOUT_BUFFER_PCT)
        sell_level = base_low * (1 - BREAKOUT_BUFFER_PCT)
        side: str | None = None
        if trigger.close > buy_level and trigger.close > trigger.open:
            side = "BUY"
        elif trigger.close < sell_level and trigger.close < trigger.open:
            side = "SELL"
        if not side:
            continue

        candidate = {
            "side": side,
            "trigger": trigger,
            "base_len": base_len,
            "base_high": base_high,
            "base_low": base_low,
            "range_pct": range_pct * 100,
            "base_avg_volume": average_volume,
            "base_avg_quote_volume": mean(c.quote_volume for c in base),
            "volume_multiple": volume_multiple,
        }
        if best is None or candidate["base_len"] > best["base_len"]:
            best = candidate

    return best


def send_alert(pair_info: dict[str, str], setup: dict[str, Any], ticker: dict[str, Any]) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN or CHAT_ID missing")

    trigger: Candle = setup["trigger"]
    side = setup["side"]
    icon = "🟢" if side == "BUY" else "🔴"
    direction = "BREAKOUT" if side == "BUY" else "BREAKDOWN"

    try:
        last_price = float(ticker.get("last_price") or trigger.close)
    except (TypeError, ValueError):
        last_price = trigger.close
    try:
        base_asset_volume = float(ticker.get("volume") or 0)
    except (TypeError, ValueError):
        base_asset_volume = 0.0
    total_24h_quote_volume = base_asset_volume * last_price

    candle_time = datetime.fromtimestamp(trigger.timestamp / 1000, tz=timezone.utc).astimezone(IST)
    message = (
        f"{icon} CoinDCX SIDEWAYS {direction}\n\n"
        f"🪙 Coin: {pair_info['name']}\n"
        f"💰 Current Price: {human_price(last_price)}\n"
        f"📊 24h Coin Volume: {human_money(total_24h_quote_volume)}\n\n"
        f"📦 Sideways Candles: {setup['base_len']}\n"
        f"📈 Sideways Range: {setup['range_pct']:.2f}%\n"
        f"⬆️ Range High: {human_price(setup['base_high'])}\n"
        f"⬇️ Range Low: {human_price(setup['base_low'])}\n\n"
        f"📊 Base Avg Candle Volume: {human_money(setup['base_avg_quote_volume'])}\n"
        f"📊 Breakout Candle Volume: {human_money(trigger.quote_volume)}\n"
        f"🔥 Volume Spike: {setup['volume_multiple']:.2f}x\n\n"
        f"🕒 Candle Time: {candle_time:%d-%m-%Y %I:%M %p} IST\n"
        f"🚀 Signal: {side}"
    )

    response = SESSION.post(
        TELEGRAM_URL.format(token=BOT_TOKEN),
        data={"chat_id": CHAT_ID, "text": message},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def scan_once(state: set[str]) -> int:
    pairs = active_usdt_pairs()
    tickers = ticker_map()
    LOGGER.info("Scanning %d active CoinDCX USDT pairs on %s", len(pairs), TIMEFRAME)
    alerts = 0

    for index, pair_info in enumerate(pairs, start=1):
        try:
            candles = fetch_closed_candles(pair_info["pair"])
            setup = find_setup(candles)
            if not setup:
                continue
            trigger: Candle = setup["trigger"]
            key = f"{pair_info['market']}:{setup['side']}:{trigger.timestamp}"
            if key in state:
                continue
            send_alert(pair_info, setup, tickers.get(pair_info["market"], {}))
            state.add(key)
            save_state(state)
            alerts += 1
            LOGGER.info("Alert sent: %s %s candles=%d volume=%.2fx", pair_info["name"], setup["side"], setup["base_len"], setup["volume_multiple"])
        except Exception as exc:
            LOGGER.warning("%s failed: %s", pair_info["pair"], exc)
        finally:
            if index < len(pairs):
                time.sleep(PAIR_DELAY_SECONDS)

    LOGGER.info("Sideways setups alerted: %d", alerts)
    return alerts


def main() -> None:
    state = load_state()
    LOGGER.info(
        "Starting CoinDCX Sideways 50+ Scanner: timeframe=%s min_sideways=%d volume=%.1fx interval=%ds",
        TIMEFRAME,
        MIN_SIDEWAYS_CANDLES,
        MIN_VOLUME_MULTIPLE,
        SCAN_INTERVAL_SECONDS,
    )
    while True:
        started = time.time()
        try:
            scan_once(state)
        except Exception as exc:
            LOGGER.exception("Scan failed: %s", exc)
        wait = max(1, SCAN_INTERVAL_SECONDS - int(time.time() - started))
        LOGGER.info("Next scan in %d seconds", wait)
        time.sleep(wait)


if __name__ == "__main__":
    main()
