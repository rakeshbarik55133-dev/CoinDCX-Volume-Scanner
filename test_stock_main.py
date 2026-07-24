import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import stock_main


class StockMarketHoursGuardTests(unittest.TestCase):
    def test_sleep_until_market_open_logs_required_message_and_sleeps_to_next_open(self) -> None:
        moments = [
            datetime(2026, 7, 24, 20, 0, tzinfo=stock_main.IST),
            datetime(2026, 7, 24, 20, 0, tzinfo=stock_main.IST),
        ]

        with patch("stock_main.now_ist", side_effect=moments), patch("stock_main.time.sleep") as sleep_mock:
            with self.assertLogs("stock_main", level="INFO") as logs:
                stock_main.sleep_until_market_open()

        self.assertIn("Outside market hours. Next scan at 09:00 IST.", logs.output[0])
        sleep_mock.assert_called_once_with(13 * 60 * 60)

    def test_run_scan_outside_market_hours_does_not_fetch_or_evaluate(self) -> None:
        session = Mock()

        with patch("stock_main.is_market_open", return_value=False), \
            patch("stock_main.sleep_until_market_open") as sleep_mock, \
            patch("stock_main.get_stock_candles") as candles_mock, \
            patch("stock_main.detect_stock_signal") as detect_mock:
            stock_main.run_scan(session, set(), ["AAPL"])

        sleep_mock.assert_called_once_with()
        candles_mock.assert_not_called()
        detect_mock.assert_not_called()
        session.get.assert_not_called()

    def test_run_does_not_load_symbols_before_market_open(self) -> None:
        with patch("stock_main.requests.Session"), \
            patch("stock_main.load_state", return_value=set()), \
            patch("stock_main.is_market_open", return_value=False), \
            patch("stock_main.sleep_until_market_open", side_effect=RuntimeError("stop")), \
            patch("stock_main.configured_symbols") as configured_symbols_mock:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                stock_main.run()

        configured_symbols_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
