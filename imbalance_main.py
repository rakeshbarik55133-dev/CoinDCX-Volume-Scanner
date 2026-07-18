"""REAL CoinDCX aggressor-volume imbalance websocket screener.

Runs continuously, aggregates genuine executed trades from CoinDCX websocket
trade messages, tracks 60%+ candidates, sends one 65%+ watchlist alert, and
sends final 70%+ alerts only after a completed 15-minute sideways-base breakout.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import socketio

COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
COINDCX_WS_URL = os.getenv("COINDCX_WS_URL", "https://stream.coindcx.com")

BASE_INTERVAL_SECONDS = 15 * 60
MIN_SIDEWAYS_CANDLES = 48
CANDIDATE_IMBALANCE_PERCENT = 60.0
WATCHLIST_IMBALANCE_PERCENT = 65.0
FINAL_IMBALANCE_PERCENT = 70.0
MAINTENANCE_INTERVAL_SECONDS = 3600
CANDIDATE_EXPIRY_SECONDS = int(os.getenv("CANDIDATE_EXPIRY_SECONDS", str(6 * 3600)))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
MAX_RECONNECT_DELAY_SECONDS = int(os.getenv("MAX_RECONNECT_DELAY_SECONDS", "300"))
MIN_TRADE_COUNT = int(os.getenv("MIN_TRADE_COUNT", "20"))
MIN_TRADE_VALUE_USDT = float(os.getenv("MIN_TRADE_VALUE_USDT", "10000"))
MAX_BASE_RANGE_PCT = float(os.getenv("MAX_BASE_RANGE_PCT", "0.018"))
MAX_BASE_DRIFT_PCT = float(os.getenv("MAX_BASE_DRIFT_PCT", "0.008"))
STATE_DB = Path(os.getenv("IMBALANCE_STATE_DB", "imbalance_state.sqlite3"))
PAIR_REFRESH_SECONDS = MAINTENANCE_INTERVAL_SECONDS
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("imbalance")


@dataclass(frozen=True)
class PairInfo:
    pair: str
    channel_pair: str


@dataclass
class Bucket:
    pair: str
    start: int
    open: float | None = None
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trade_value: float = 0.0
    trade_count: int = 0
    complete: bool = True

    def add_trade(self, price: float, quantity: float, is_buyer_maker: bool) -> None:
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if is_buyer_maker:
            self.sell_volume += quantity
        else:
            self.buy_volume += quantity
        self.trade_value += price * quantity
        self.trade_count += 1

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def buy_pct(self) -> float:
        return 0.0 if self.total_volume <= 0 else self.buy_volume / self.total_volume * 100

    @property
    def sell_pct(self) -> float:
        return 0.0 if self.total_volume <= 0 else self.sell_volume / self.total_volume * 100


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidates(
                  pair TEXT NOT NULL, side TEXT NOT NULL, setup_id TEXT NOT NULL,
                  first_detected_time INTEGER NOT NULL, latest_imbalance_percentage REAL NOT NULL,
                  sideways_base_length INTEGER NOT NULL, base_high REAL NOT NULL, base_low REAL NOT NULL,
                  watchlist_sent INTEGER NOT NULL DEFAULT 0, final_alert_sent INTEGER NOT NULL DEFAULT 0,
                  last_updated_time INTEGER NOT NULL, expiry_time INTEGER NOT NULL, validity_status TEXT NOT NULL,
                  PRIMARY KEY(pair, side, setup_id));
                CREATE TABLE IF NOT EXISTS alert_dedupe(kind TEXT NOT NULL, alert_key TEXT NOT NULL, sent_time INTEGER NOT NULL, PRIMARY KEY(kind, alert_key));
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS incomplete_buckets(pair TEXT NOT NULL, bucket_start INTEGER NOT NULL, reason TEXT NOT NULL, marked_time INTEGER NOT NULL, PRIMARY KEY(pair, bucket_start));
                CREATE TABLE IF NOT EXISTS trade_dedupe(pair TEXT NOT NULL, trade_id TEXT NOT NULL, seen_time INTEGER NOT NULL, PRIMARY KEY(pair, trade_id));
                """
            )

    def save_candidate(self, bucket: Bucket, side: str, base_len: int, base_high: float, base_low: float, pct: float) -> str:
        now = int(time.time())
        setup_id = f"{bucket.pair}:{side}:{bucket.start}:{base_high:.12g}:{base_low:.12g}"
        with self.lock, self.conn:
            opposite = "SELL" if side == "BUY" else "BUY"
            self.conn.execute("UPDATE candidates SET validity_status='reversed', expiry_time=? WHERE pair=? AND side=? AND validity_status='active'", (now, bucket.pair, opposite))
            self.conn.execute(
                """INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pair,side,setup_id) DO UPDATE SET latest_imbalance_percentage=excluded.latest_imbalance_percentage,
                   sideways_base_length=excluded.sideways_base_length, base_high=excluded.base_high, base_low=excluded.base_low,
                   last_updated_time=excluded.last_updated_time, expiry_time=excluded.expiry_time, validity_status='active'""",
                (bucket.pair, side, setup_id, now, pct, base_len, base_high, base_low, 0, 0, now, now + CANDIDATE_EXPIRY_SECONDS, "active"),
            )
        return setup_id

    def dedupe(self, kind: str, key: str) -> bool:
        with self.lock, self.conn:
            try:
                self.conn.execute("INSERT INTO alert_dedupe VALUES(?,?,?)", (kind, key, int(time.time())))
                column = "watchlist_sent" if kind == "watchlist" else "final_alert_sent" if kind == "final" else None
                if column:
                    self.conn.execute(f"UPDATE candidates SET {column}=1 WHERE setup_id=?", (key,))
                return True
            except sqlite3.IntegrityError:
                return False

    def seen_trade(self, pair: str, trade_id: str | None) -> bool:
        if not trade_id:
            return False
        with self.lock, self.conn:
            try:
                self.conn.execute("INSERT INTO trade_dedupe VALUES(?,?,?)", (pair, trade_id, int(time.time())))
                return False
            except sqlite3.IntegrityError:
                return True

    def mark_incomplete(self, pairs: set[str], bucket_start: int, reason: str) -> None:
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO incomplete_buckets VALUES(?,?,?,?)", [(p, bucket_start, reason, int(time.time())) for p in pairs])

    def is_incomplete(self, pair: str, bucket_start: int) -> bool:
        return self.conn.execute("SELECT 1 FROM incomplete_buckets WHERE pair=? AND bucket_start=?", (pair, bucket_start)).fetchone() is not None

    def expire_stale(self) -> int:
        now = int(time.time())
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE candidates SET validity_status='expired' WHERE validity_status='active' AND expiry_time<=?", (now,))
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        return {
            "candidates": self.conn.execute("SELECT COUNT(*) FROM candidates WHERE validity_status='active'").fetchone()[0],
            "watchlists": self.conn.execute("SELECT COUNT(*) FROM alert_dedupe WHERE kind='watchlist'").fetchone()[0],
            "finals": self.conn.execute("SELECT COUNT(*) FROM alert_dedupe WHERE kind='final'").fetchone()[0],
            "incomplete": self.conn.execute("SELECT COUNT(*) FROM incomplete_buckets").fetchone()[0],
        }

    def set_kv(self, key: str, value: Any) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value)))


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        LOGGER.info("telegram disabled; message would be:\n%s", text)
        return
    requests.post(TELEGRAM_URL.format(token=token), json={"chat_id": chat_id, "text": text}, timeout=REQUEST_TIMEOUT).raise_for_status()


def fetch_usdt_pairs() -> dict[str, PairInfo]:
    rows = requests.get(COINDCX_MARKETS_URL, timeout=REQUEST_TIMEOUT).json()
    pairs: dict[str, PairInfo] = {}
    for row in rows:
        quote = str(row.get("quote_currency_short_name") or row.get("target_currency_short_name") or "").upper()
        status = str(row.get("status") or "active").lower()
        if quote == "USDT" and status in {"active", "online", "enabled"}:
            pair = str(row.get("symbol") or row.get("pair") or row.get("coindcx_name") or "").replace("_", "/")
            channel_pair = str(row.get("coindcx_name") or row.get("pair") or pair.replace("/", "_"))
            if pair and channel_pair:
                pairs[pair] = PairInfo(pair, channel_pair)
    return pairs


class Screener:
    def __init__(self) -> None:
        self.store = StateStore(STATE_DB)
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self.pairs: dict[str, PairInfo] = {}
        self.subscribed: set[str] = set()
        self.buckets: dict[str, Bucket] = {}
        self.history: dict[str, list[Bucket]] = {}
        self.connected = False
        self.stop = threading.Event()
        self._wire_events()

    def _wire_events(self) -> None:
        @self.sio.event
        def connect() -> None:
            self.connected = True
            LOGGER.info("websocket connected")
            self.refresh_pairs_and_subscriptions()

        @self.sio.event
        def disconnect() -> None:
            self.connected = False
            current = int(time.time()) // BASE_INTERVAL_SECONDS * BASE_INTERVAL_SECONDS
            self.store.mark_incomplete(set(self.pairs), current, "websocket disconnect")
            LOGGER.warning("websocket disconnected; current buckets marked incomplete")

        @self.sio.on("trade-update")
        def trade_update(data: Any) -> None:
            self.handle_trade_payload(data)

    def refresh_pairs_and_subscriptions(self) -> None:
        self.pairs = fetch_usdt_pairs()
        self.store.set_kv("pair_universe_refresh_time", int(time.time()))
        channels = {f"{info.channel_pair}@trades" for info in self.pairs.values()}
        for channel in channels - self.subscribed:
            self.sio.emit("join", {"channelName": channel})
        for channel in self.subscribed - channels:
            self.sio.emit("leave", {"channelName": channel})
        self.subscribed = channels
        LOGGER.info("subscriptions reconciled: active_pairs=%d subscribed=%d", len(self.pairs), len(self.subscribed))

    def handle_trade_payload(self, data: Any) -> None:
        if isinstance(data, str):
            data = json.loads(data)
        trades = data if isinstance(data, list) else data.get("data", data.get("trades", [data]))
        for trade in trades if isinstance(trades, list) else [trades]:
            self.handle_trade(trade)

    def handle_trade(self, trade: dict[str, Any]) -> None:
        raw_pair = str(trade.get("s") or trade.get("symbol") or trade.get("pair") or "").replace("_", "/")
        pair = next((p for p, info in self.pairs.items() if raw_pair in {p, info.channel_pair.replace("_", "/")}), raw_pair)
        if pair not in self.pairs:
            return
        trade_id = str(trade.get("t") or trade.get("trade_id") or trade.get("id") or "") or None
        if self.store.seen_trade(pair, trade_id):
            return
        price = float(trade.get("p") or trade.get("price"))
        qty = float(trade.get("q") or trade.get("quantity") or trade.get("volume"))
        ts_ms = int(trade.get("T") or trade.get("timestamp") or time.time() * 1000)
        is_buyer_maker = bool(trade.get("m"))
        start = ts_ms // 1000 // BASE_INTERVAL_SECONDS * BASE_INTERVAL_SECONDS
        bucket = self.buckets.get(pair)
        if bucket and bucket.start != start:
            self.finalize_bucket(bucket)
            bucket = None
        if bucket is None:
            bucket = self.buckets[pair] = Bucket(pair, start, complete=not self.store.is_incomplete(pair, start))
        bucket.add_trade(price, qty, is_buyer_maker)

    def finalize_bucket(self, bucket: Bucket) -> None:
        self.history.setdefault(bucket.pair, []).append(bucket)
        self.history[bucket.pair] = self.history[bucket.pair][-80:]
        if not bucket.complete:
            return
        side, pct = ("BUY", bucket.buy_pct) if bucket.buy_pct >= bucket.sell_pct else ("SELL", bucket.sell_pct)
        if pct < CANDIDATE_IMBALANCE_PERCENT or bucket.trade_count < MIN_TRADE_COUNT or bucket.trade_value < MIN_TRADE_VALUE_USDT:
            return
        base = self.sideways_base(bucket.pair)
        if not base:
            return
        base_len, base_high, base_low = base
        setup_id = self.store.save_candidate(bucket, side, base_len, base_high, base_low, pct)
        exact = f"BUY {bucket.buy_pct:.1f}% vs SELL {bucket.sell_pct:.1f}%"
        reverse = f"SELL {bucket.sell_pct:.1f}% vs BUY {bucket.buy_pct:.1f}%"
        if pct >= WATCHLIST_IMBALANCE_PERCENT and self.store.dedupe("watchlist", setup_id):
            label = exact if side == "BUY" else reverse
            action = "breakout" if side == "BUY" else "breakdown"
            send_telegram(f"🟡 REAL IMBALANCE WATCHLIST\n\nPair: {bucket.pair}\nVolume Side: {side} 65:35 or stronger — {label}\nStatus: Watching for sideways-range {action}\nSideways Base: {base_len} candles\nReal Buy Volume: {bucket.buy_volume:.8g}\nReal Sell Volume: {bucket.sell_volume:.8g}\nTotal Trade Value: {bucket.trade_value:.2f} USDT\nTrade Count: {bucket.trade_count}\nCandle Time: {datetime.fromtimestamp(bucket.start, IST):%Y-%m-%d %H:%M:%S IST}")
        breakout = bucket.close > base_high if side == "BUY" else bucket.close < base_low
        if pct >= FINAL_IMBALANCE_PERCENT and breakout and self.store.dedupe("final", setup_id):
            label = exact if side == "BUY" else reverse
            icon = "🟢 REAL BUY IMBALANCE" if side == "BUY" else "🔴 REAL SELL IMBALANCE"
            send_telegram(f"{icon} — {label}\n\nPair: {bucket.pair}\nVolume Imbalance: {side} 70:30+\nSideways Base: {base_len} candles\nBase High: {base_high}\nBase Low: {base_low}\nTrigger Close: {bucket.close}\nReal Buy Volume: {bucket.buy_volume:.8g}\nReal Sell Volume: {bucket.sell_volume:.8g}\nTotal Trade Value: {bucket.trade_value:.2f} USDT\nTrade Count: {bucket.trade_count}\nCandle Time: {datetime.fromtimestamp(bucket.start, IST):%Y-%m-%d %H:%M:%S IST}")

    def sideways_base(self, pair: str) -> tuple[int, float, float] | None:
        candles = self.history.get(pair, [])[-MIN_SIDEWAYS_CANDLES:]
        if len(candles) < MIN_SIDEWAYS_CANDLES or any(not c.complete for c in candles):
            return None
        highs, lows = [c.high for c in candles], [c.low for c in candles]
        high, low = max(highs), min(lows)
        mid = (high + low) / 2
        if mid <= 0 or (high - low) / mid > MAX_BASE_RANGE_PCT:
            return None
        first_mid = (candles[0].open + candles[0].close) / 2 if candles[0].open else candles[0].close
        if first_mid and abs(candles[-1].close - first_mid) / first_mid > MAX_BASE_DRIFT_PCT:
            return None
        return len(candles), high, low

    def connect_forever(self) -> None:
        delay = 1
        while not self.stop.is_set():
            try:
                if not self.connected:
                    self.pairs = fetch_usdt_pairs()
                    self.sio.connect(COINDCX_WS_URL, transports=["websocket"])
                delay = 1
                time.sleep(1)
            except Exception as exc:
                self.connected = False
                current = int(time.time()) // BASE_INTERVAL_SECONDS * BASE_INTERVAL_SECONDS
                self.store.mark_incomplete(set(self.pairs), current, f"reconnect: {exc}")
                LOGGER.warning("websocket reconnect in %ss: %s", delay, exc)
                time.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY_SECONDS)

    def maintenance_forever(self) -> None:
        while not self.stop.wait(MAINTENANCE_INTERVAL_SECONDS):
            expired = self.store.expire_stale()
            if self.connected:
                self.refresh_pairs_and_subscriptions()
            else:
                try:
                    self.sio.disconnect()
                except Exception:
                    pass
            counts = self.store.counts()
            LOGGER.info("hourly summary active_pairs=%d subscribed_pairs=%d saved_60_candidates=%d watchlist_setups=%d final_alerts_sent=%d incomplete_buckets=%d websocket_status=%s expired=%d", len(self.pairs), len(self.subscribed), counts["candidates"], counts["watchlists"], counts["finals"], counts["incomplete"], "connected" if self.connected else "disconnected", expired)

    def run(self) -> None:
        LOGGER.info("starting REAL CoinDCX imbalance screener")
        threading.Thread(target=self.maintenance_forever, daemon=True).start()
        self.connect_forever()


def main() -> None:
    screener = Screener()
    signal.signal(signal.SIGTERM, lambda *_: screener.stop.set())
    signal.signal(signal.SIGINT, lambda *_: screener.stop.set())
    screener.run()


if __name__ == "__main__":
    main()
