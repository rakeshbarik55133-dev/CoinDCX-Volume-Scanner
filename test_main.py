import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from main import (
    BASE_INTERVAL,
    TRIGGER_INTERVAL,
    BaseSetup,
    Candle,
    detect_signal,
    evaluate_signal,
    evaluate_trigger,
    find_latest_sideways_base,
    get_candles,
    get_usdt_pairs,
    load_state,
    save_state,
)


def candle(open_price: float, high: float, low: float, close: float, volume: float, timestamp: int) -> Candle:
    return Candle(timestamp=timestamp, open=open_price, high=high, low=low, close=close, volume=volume)


class CoinDCXCandleFetchTests(unittest.TestCase):
    def test_get_usdt_pairs_uses_active_usdt_market_details_pair_for_candle_api(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict[str, str]]:
                return [
                    {"coindcx_name": "BTCUSDT", "target_currency_short_name": "USDT", "pair": "B-BTC_USDT", "status": "active"},
                    {"coindcx_name": "ETHUSDT", "target_currency_short_name": "USDT", "pair": "B-ETH_USDT", "status": "inactive"},
                    {"coindcx_name": "ETHBTC", "target_currency_short_name": "BTC", "pair": "B-ETH_BTC", "status": "active"},
                ]

        class Session:
            def get(self, *args, **kwargs) -> Response:
                return Response()

        self.assertEqual(get_usdt_pairs(Session()), ["B-BTC_USDT"])

    def test_get_candles_parses_descending_api_candles_chronologically_and_interval(self) -> None:
        class Response:
            status_code = 200
            text = "[]"

            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict[str, str]]:
                return [
                    {"open": "2", "high": "3", "low": "1", "close": "2.5", "volume": "10", "time": "2000"},
                    {"open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "8", "time": "1000"},
                ]

        class Session:
            params: dict[str, object]

            def get(self, _url, params, timeout) -> Response:
                self.params = params
                return Response()

        session = Session()
        candles = get_candles(session, "B-BTC_USDT", TRIGGER_INTERVAL)

        self.assertEqual(session.params["pair"], "B-BTC_USDT")
        self.assertEqual(session.params["interval"], "5m")
        self.assertEqual([item.timestamp for item in candles], [1000, 2000])
        self.assertEqual(candles[0].close, 1.5)

    def test_get_candles_logs_sample_status_and_body_at_info(self) -> None:
        class Response:
            status_code = 200
            text = '[{"time":"1000"}]'

            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict[str, str]]:
                return [{"open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "time": "1000"}]

        class Session:
            def get(self, _url, params, timeout) -> Response:
                return Response()

        with self.assertLogs("main", level="INFO") as logs:
            get_candles(Session(), "B-BTC_USDT", BASE_INTERVAL, log_response=True)

        self.assertIn("15m candle response", logs.output[0])
        self.assertIn("status=200", logs.output[0])


class ImmediateFiveMinuteTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now_ms = 1_700_020_000_000

    def base_candles(self) -> list[Candle]:
        start = self.now_ms - (12 * 900_000) - 900_000
        candles: list[Candle] = []
        for index in range(12):
            price = 100 + (index % 3) * 0.05
            candles.append(candle(price, 100.45, 99.75, price + 0.02, 50 + (index % 2), start + index * 900_000))
        return candles

    def latest_base(self) -> BaseSetup:
        with patch("main.time.time", return_value=self.now_ms / 1000):
            setup = find_latest_sideways_base("BLURUSDT", self.base_candles())
        self.assertIsNotNone(setup)
        return setup  # type: ignore[return-value]

    def test_saves_original_15m_base_levels_and_reference_volume(self) -> None:
        setup = self.latest_base()
        self.assertEqual(setup.base_high, 100.45)
        self.assertEqual(setup.base_low, 99.75)
        self.assertAlmostEqual(setup.reference_volume, 50.5)

    def test_buy_alerts_on_running_5m_high_break_and_3x_original_volume_without_close_wait(self) -> None:
        setup = self.latest_base()
        running = candle(100.0, 100.6, 99.9, 100.2, setup.reference_volume * 3, self.now_ms)
        signal = evaluate_trigger("BLURUSDT", setup, running, self.now_ms).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.break_level, setup.base_high)

    def test_sell_alerts_on_running_5m_low_break_and_3x_original_volume_without_close_wait(self) -> None:
        setup = self.latest_base()
        running = candle(100.0, 100.1, 99.5, 99.9, setup.reference_volume * 3, self.now_ms)
        signal = evaluate_trigger("MUSDT", setup, running, self.now_ms).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "SELL")
        self.assertEqual(signal.break_level, setup.base_low)

    def test_rejects_break_when_running_volume_is_less_than_3x_reference(self) -> None:
        setup = self.latest_base()
        running = candle(100.0, 101.0, 99.9, 100.8, setup.reference_volume * 2.99, self.now_ms)
        evaluation = evaluate_trigger("NOSPIKEUSDT", setup, running, self.now_ms)
        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "volume_spike_too_small")

    def test_missed_scan_recovery_alerts_when_next_scan_price_still_beyond_original_high(self) -> None:
        setup = self.latest_base()
        later_running = candle(100.7, 100.9, 100.55, 100.58, setup.reference_volume * 3.2, self.now_ms + 60_000)
        signal = evaluate_trigger("RECOVERYUSDT", setup, later_running, self.now_ms + 60_000).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.setup.base_high, 100.45)

    def test_later_candles_do_not_replace_original_base_values_inside_existing_setup(self) -> None:
        setup = self.latest_base()
        later_running = candle(104, 105, 103, 104.5, setup.reference_volume * 4, self.now_ms)
        signal = evaluate_trigger("ORIGINALUSDT", setup, later_running, self.now_ms).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.setup.base_high, 100.45)
        self.assertEqual(signal.setup.base_low, 99.75)
        self.assertAlmostEqual(signal.setup.reference_volume, 50.5)

    def test_detect_signal_uses_15m_base_and_latest_running_5m_candle_only(self) -> None:
        trigger = [candle(100, 100.2, 99.9, 100.1, 10, self.now_ms - 300_000), candle(100, 100.7, 99.9, 100.3, 200, self.now_ms)]
        with patch("main.time.time", return_value=self.now_ms / 1000):
            signal = detect_signal("FASTUSDT", self.base_candles(), trigger)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.candle.timestamp, self.now_ms)

    def test_evaluate_signal_reports_no_sideways_base_without_adding_other_filters(self) -> None:
        candles = self.base_candles()
        candles[-1] = candle(100, 110, 90, 101, 50, candles[-1].timestamp)
        with patch("main.time.time", return_value=self.now_ms / 1000):
            evaluation = evaluate_signal("CHOPUSDT", candles, [candle(100, 111, 99, 110, 300, self.now_ms)])
        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "no_sideways_base")

    def test_persists_duplicate_suppression_and_setups(self) -> None:
        setup = self.latest_base()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch.object(main, "STATE_FILE", state_path):
                save_state({"alert-key"}, {"BLURUSDT": setup})
                state = load_state()
        self.assertEqual(state["sent_alerts"], {"alert-key"})
        self.assertEqual(state["setups"]["BLURUSDT"]["base_high"], setup.base_high)

    def test_setup_expires(self) -> None:
        setup = self.latest_base()
        running = candle(100, 101, 99, 100.8, setup.reference_volume * 4, setup.expires_at + 1)
        evaluation = evaluate_trigger("EXPIREUSDT", setup, running, setup.expires_at + 1)
        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "setup_expired")


if __name__ == "__main__":
    unittest.main()
