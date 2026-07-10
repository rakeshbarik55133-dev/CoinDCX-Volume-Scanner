import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import fyers_data_collector as collector
from fyers_data_collector import FyersCandle


class FyersDataCollectorTests(unittest.TestCase):
    def test_access_token_is_read_directly_from_environment(self) -> None:
        with patch.dict(os.environ, {"FYERS_ACCESS_TOKEN": "app-id:secret-token"}, clear=True):
            self.assertEqual(collector.get_access_token(), "app-id:secret-token")

    def test_missing_access_token_raises_without_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FYERS_ACCESS_TOKEN"):
                collector.get_access_token()

    def test_configured_symbols_are_uppercase_unique_and_sorted(self) -> None:
        with patch.dict(os.environ, {"FYERS_SYMBOLS": "nse:sbin-eq, NSE:RELIANCE-EQ, nse:sbin-eq"}):
            self.assertEqual(
                collector.configured_symbols(),
                ["NSE:RELIANCE-EQ", "NSE:SBIN-EQ"],
            )

    def test_database_schema_prevents_duplicate_symbol_timestamp_candles(self) -> None:
        connection = sqlite3.connect(":memory:")
        collector.initialize_database(connection)

        candles = [
            FyersCandle("NSE:SBIN-EQ", 1_700_000_000, 1, 2, 0.5, 1.5, 100),
            FyersCandle("NSE:SBIN-EQ", 1_700_000_000, 2, 3, 1.5, 2.5, 200),
        ]
        collector.store_candles(connection, candles)

        rows = connection.execute(
            "SELECT symbol, timestamp, open, close, volume FROM fyers_ohlcv_5m"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ("NSE:SBIN-EQ", 1_700_000_000, 2.0, 2.5, 200.0))

    def test_incremental_start_date_uses_latest_stored_candle_date(self) -> None:
        connection = sqlite3.connect(":memory:")
        collector.initialize_database(connection)
        collector.store_candles(
            connection,
            [FyersCandle("NSE:SBIN-EQ", 1_704_067_200, 1, 2, 0.5, 1.5, 100)],
        )

        start = collector.incremental_start_date(
            connection, "NSE:SBIN-EQ", dt.date(2023, 12, 1)
        )

        self.assertEqual(start, dt.date(2024, 1, 1))

    def test_fetch_history_uses_fyers_v3_history_parameters_and_token_header(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {"s": "ok", "candles": [[1704067200, 1, 2, 0.5, 1.5, 100]]}

        class Session:
            def get(self, url, headers, params, timeout):
                self.url = url
                self.headers = headers
                self.params = params
                self.timeout = timeout
                return Response()

        session = Session()
        candles = collector.fetch_history(
            session,
            "direct-token",
            "NSE:SBIN-EQ",
            dt.date(2024, 1, 1),
            dt.date(2024, 1, 2),
        )

        self.assertEqual(session.url, collector.FYERS_HISTORY_URL)
        self.assertEqual(session.headers["Authorization"], "direct-token")
        self.assertEqual(session.params["symbol"], "NSE:SBIN-EQ")
        self.assertEqual(session.params["resolution"], "5")
        self.assertEqual(session.params["date_format"], "1")
        self.assertEqual(session.params["range_from"], "2024-01-01")
        self.assertEqual(session.params["range_to"], "2024-01-02")
        self.assertEqual(candles[0].close, 1.5)

    def test_connect_database_creates_sqlite_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "fyers.sqlite3")
            connection = collector.connect_database(db_path)
            connection.close()
            self.assertTrue(os.path.exists(db_path))


if __name__ == "__main__":
    unittest.main()
