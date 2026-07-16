import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

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
    format_utc_timestamp,
    get_candles,
    get_latest_trigger_candle,
    get_usdt_pairs,
    remember_invalid_candle_pair,
    load_state,
    save_state,
    wait_for_next_scan,
)


def candle(open_price: float, high: float, low: float, close: float, volume: float, timestamp: int) -> Candle:
    return Candle(timestamp=timestamp, open=open_price, high=high, low=low, close=close, volume=volume)


class CoinDCXCandleFetchTests(unittest.TestCase):
    def test_full_market_refresh_runs_hourly(self) -> None:
        self.assertEqual(main.PAIR_REFRESH_SECONDS, 60 * 60)

    def test_get_usdt_pairs_uses_active_usdt_market_details_pair_for_candle_api(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict[str, str]]:
                return [
                    {"coindcx_name": "BTCUSDT", "target_currency_short_name": "USDT", "pair": "B-BTC_USDT", "status": "active", "ecode": "B"},
                    {"coindcx_name": "ETHUSDT", "target_currency_short_name": "USDT", "pair": "B-ETH_USDT", "status": "inactive", "ecode": "B"},
                    {"coindcx_name": "ETHBTC", "target_currency_short_name": "BTC", "pair": "B-ETH_BTC", "status": "active", "ecode": "B"},
                    {"coindcx_name": "SOLUSDT", "target_currency_short_name": "USDT", "pair": "I-SOL_USDT", "status": "active", "ecode": "I"},
                    {"coindcx_name": "XRPUSDT", "quote_currency_short_name": "USDT", "pair": "B-XRP_USDT", "status": "active", "ecode": "B"},
                    {"coindcx_name": "ADAUSDT", "target_currency_short_name": "BTC", "pair": "B-ADA_USDT", "status": "active", "ecode": "B"},
                    {"coindcx_name": "DOGEUSDT", "target_currency_short_name": "USDT", "pair": "B-DOGE_USDT", "status": "online", "ecode": "B"},
                ]

        class Session:
            def get(self, *args, **kwargs) -> Response:
                return Response()

        self.assertEqual(get_usdt_pairs(Session()), ["B-BTC_USDT"])
        self.assertEqual(main.ALERT_PAIR_NAMES["B-BTC_USDT"], "BTCUSDT")

    def test_get_usdt_pairs_uses_candles_endpoint_symbol_and_skips_cached_invalid_pair(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict[str, str]]:
                return [
                    {"coindcx_name": "1000CHEEMSUSDT", "target_currency_short_name": "USDT", "pair": "B-1000CHEEMS_USDT", "status": "active", "ecode": "B"},
                    {"coindcx_name": "BTCUSDT", "target_currency_short_name": "USDT", "pair": "B-BTC_USDT", "status": "active", "ecode": "B"},
                ]

        class Session:
            def get(self, *args, **kwargs) -> Response:
                return Response()

        with patch.object(main, "INVALID_CANDLE_PAIRS", {"B-1000CHEEMS_USDT"}), patch.object(main, "ALERT_PAIR_NAMES", {}):
            self.assertEqual(get_usdt_pairs(Session()), ["B-BTC_USDT"])

    def test_remember_invalid_candle_pair_logs_once_and_caches_pair(self) -> None:
        with patch.object(main, "INVALID_CANDLE_PAIRS", set()), patch.object(main, "LOGGED_INVALID_CANDLE_PAIRS", set()):
            with self.assertLogs("main", level="WARNING") as logs:
                remember_invalid_candle_pair("ARBUSDT", TRIGGER_INTERVAL)
                remember_invalid_candle_pair("ARBUSDT", TRIGGER_INTERVAL)

            self.assertEqual(main.INVALID_CANDLE_PAIRS, {"ARBUSDT"})
            self.assertEqual(len(logs.output), 1)
            self.assertIn("excluding for the rest of this run", logs.output[0])

    def test_get_candles_propagates_422_with_response_for_invalid_pair_cache(self) -> None:
        class Response:
            status_code = 422
            text = "invalid pair"

            def raise_for_status(self) -> None:
                raise requests.HTTPError("422 Client Error", response=self)

        class Session:
            def get(self, _url, params, timeout) -> Response:
                return Response()

        with self.assertRaises(requests.HTTPError) as raised:
            get_candles(Session(), "B-ARB_USDT", TRIGGER_INTERVAL)

        self.assertEqual(raised.exception.response.status_code, 422)

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
        self.assertEqual(session.params["interval"], "15m")
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



class ForeverLoopSchedulingTests(unittest.TestCase):
    def test_next_scan_is_scheduled_one_hour_after_completed_scan(self) -> None:
        completed_at = 1_700_000_000.0
        self.assertEqual(format_utc_timestamp(completed_at + main.FULL_SCAN_DELAY_SECONDS), "2023-11-14 23:13:20 UTC")

        sleeps: list[float] = []
        current_time = completed_at

        def fake_time() -> float:
            return current_time

        def fake_sleep(seconds: float) -> None:
            nonlocal current_time
            sleeps.append(seconds)
            current_time += main.FULL_SCAN_DELAY_SECONDS

        with patch("main.time.time", side_effect=fake_time), patch("main.time.sleep", side_effect=fake_sleep):
            with self.assertLogs("main", level="INFO") as logs:
                wait_for_next_scan(completed_at)

        self.assertEqual(sleeps, [60.0])
        self.assertIn("next scan scheduled at 2023-11-14 23:13:20 UTC", logs.output[0])

    def test_late_wake_starts_next_scan_without_extra_hour(self) -> None:
        completed_at = 1_700_000_000.0
        late_wake_time = completed_at + main.FULL_SCAN_DELAY_SECONDS + 5

        with patch("main.time.time", return_value=late_wake_time), patch("main.time.sleep") as sleep_mock:
            with self.assertLogs("main", level="WARNING") as logs:
                wait_for_next_scan(completed_at)

        sleep_mock.assert_not_called()
        self.assertIn("next scan is starting 5.0 seconds late", logs.output[0])


class ImmediateFifteenMinuteTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now_ms = 1_700_020_000_000

    def base_candles(self) -> list[Candle]:
        start = self.now_ms - (main.BASE_LOOKBACK * 900_000) - 900_000
        candles: list[Candle] = []
        for index in range(main.BASE_LOOKBACK):
            price = 100 + (index % 3) * 0.05
            candles.append(candle(price, 100.45, 99.75, price + 0.02, 50 + (index % 2), start + index * 900_000))
        return candles

    def latest_base(self) -> BaseSetup:
        with patch("main.time.time", return_value=self.now_ms / 1000):
            setup = find_latest_sideways_base("BLURUSDT", self.base_candles())
        self.assertIsNotNone(setup)
        return setup  # type: ignore[return-value]

    def test_sideways_base_requires_at_least_50_consecutive_15m_candles(self) -> None:
        self.assertEqual(main.BASE_LOOKBACK, 50)
        with patch("main.time.time", return_value=self.now_ms / 1000):
            setup = find_latest_sideways_base("SHORTUSDT", self.base_candles()[:-1])
        self.assertIsNone(setup)

    def test_saves_original_15m_base_levels_and_reference_volume(self) -> None:
        setup = self.latest_base()
        self.assertEqual(setup.base_high, 100.45)
        self.assertEqual(setup.base_low, 99.75)
        self.assertAlmostEqual(setup.reference_volume, 50.5)
        self.assertAlmostEqual(setup.max_base_volume, 51)

    def test_sideways_base_allows_every_candle_at_or_below_1_6x_average_volume(self) -> None:
        candles = [
            candle(item.open, item.high, item.low, item.close, 100, item.timestamp)
            for item in self.base_candles()
        ]
        candles[-1] = candle(candles[-1].open, candles[-1].high, candles[-1].low, candles[-1].close, 160, candles[-1].timestamp)

        with patch("main.time.time", return_value=self.now_ms / 1000):
            setup = find_latest_sideways_base("FLATUSDT", candles)

        self.assertIsNotNone(setup)
        self.assertAlmostEqual(setup.reference_volume, 101.2)
        self.assertLessEqual(max(item.volume for item in candles), setup.reference_volume * main.MAX_BASE_VOLUME_VARIATION_RATIO)

    def test_sideways_base_rejects_any_candle_above_1_6x_average_volume(self) -> None:
        candles = [
            candle(item.open, item.high, item.low, item.close, 100, item.timestamp)
            for item in self.base_candles()
        ]
        candles[-1] = candle(candles[-1].open, candles[-1].high, candles[-1].low, candles[-1].close, 170, candles[-1].timestamp)

        with patch("main.time.time", return_value=self.now_ms / 1000):
            setup = find_latest_sideways_base("SPIKYUSDT", candles)

        self.assertIsNone(setup)

    def test_buy_alerts_on_latest_15m_high_break_and_3x_original_volume_without_close_wait(self) -> None:
        setup = self.latest_base()
        latest = candle(100.0, 100.6, 99.9, 100.2, setup.reference_volume * 3, self.now_ms)
        signal = evaluate_trigger("BLURUSDT", setup, latest, self.now_ms).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.break_level, setup.base_high)

    def test_sell_alerts_on_latest_15m_low_break_and_3x_original_volume_without_close_wait(self) -> None:
        setup = self.latest_base()
        latest = candle(100.0, 100.1, 99.5, 99.9, setup.reference_volume * 3, self.now_ms)
        signal = evaluate_trigger("MUSDT", setup, latest, self.now_ms).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "SELL")
        self.assertEqual(signal.break_level, setup.base_low)

    def test_close_without_high_or_low_cross_does_not_trigger(self) -> None:
        setup = self.latest_base()
        latest = candle(100.0, setup.base_high, setup.base_low, setup.base_high + 0.1, setup.reference_volume * 3, self.now_ms)
        evaluation = evaluate_trigger("CLOSEUSDT", setup, latest, self.now_ms)
        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "no_base_break")

    def test_rejects_break_when_latest_15m_volume_is_less_than_3x_reference(self) -> None:
        setup = self.latest_base()
        latest = candle(100.0, 101.0, 99.9, 100.8, setup.reference_volume * 2.99, self.now_ms)
        evaluation = evaluate_trigger("NOSPIKEUSDT", setup, latest, self.now_ms)
        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "volume_spike_too_small")

    def test_trigger_volume_does_not_need_2x_base_max_volume(self) -> None:
        candles = [
            candle(item.open, item.high, item.low, item.close, 100, item.timestamp)
            for item in self.base_candles()
        ]
        candles[-1] = candle(candles[-1].open, candles[-1].high, candles[-1].low, candles[-1].close, 160, candles[-1].timestamp)
        with patch("main.time.time", return_value=self.now_ms / 1000):
            setup = find_latest_sideways_base("MAXBASEUSDT", candles)

        self.assertIsNotNone(setup)
        assert setup is not None
        self.assertAlmostEqual(setup.reference_volume, 101.2)
        self.assertAlmostEqual(setup.max_base_volume, 160)

        exact_3x_average = candle(100, 100.7, 99.9, 100.2, setup.reference_volume * 3, self.now_ms)
        accepted = evaluate_trigger("MAXBASEUSDT", setup, exact_3x_average, self.now_ms)

        self.assertIsNotNone(accepted.signal)
        self.assertAlmostEqual(accepted.signal.volume_ratio, 3.0)

    def test_previous_candle_volume_is_ignored_for_trigger_reference(self) -> None:
        setup = self.latest_base()
        previous = candle(100, 100.2, 99.9, 100.1, 10_000, self.now_ms - 900_000)
        trigger = candle(100, 100.7, 99.9, 100.2, setup.reference_volume * 2.99, self.now_ms)

        self.assertGreater(trigger.volume, previous.volume / 100)
        evaluation = evaluate_trigger("PREVIOUSUSDT", setup, trigger, self.now_ms)

        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "volume_spike_too_small")

    def test_saved_sideways_base_average_volume_is_used_for_trigger_ratio(self) -> None:
        setup = self.latest_base()
        trigger = candle(100, 100.7, 99.9, 100.2, 151.5, self.now_ms)

        signal = evaluate_trigger("AVERAGEUSDT", setup, trigger, self.now_ms).signal

        self.assertIsNotNone(signal)
        self.assertAlmostEqual(setup.reference_volume, 50.5)
        self.assertAlmostEqual(signal.volume_ratio, 3.0)

    def test_existing_setup_reference_volume_is_not_replaced_by_later_base_average(self) -> None:
        setup = self.latest_base()
        later_start = setup.base_end_timestamp + 900_000
        later_base = [
            candle(100 + (index % 2) * 0.03, 100.4, 99.8, 100.01, 80 + (index % 2), later_start + index * 900_000)
            for index in range(main.BASE_LOOKBACK)
        ]
        with patch("main.time.time", return_value=(later_base[-1].timestamp + 900_000) / 1000):
            later_setup = find_latest_sideways_base("BLURUSDT", later_base)

        self.assertIsNotNone(later_setup)
        self.assertAlmostEqual(setup.reference_volume, 50.5)
        self.assertAlmostEqual(later_setup.reference_volume, 80.5)

        setups = {"BLURUSDT": setup}
        if later_setup and "BLURUSDT" not in setups:
            setups["BLURUSDT"] = later_setup

        self.assertIs(setups["BLURUSDT"], setup)
        self.assertAlmostEqual(setups["BLURUSDT"].reference_volume, 50.5)

    def test_alert_requires_trigger_volume_at_least_3x_saved_sideways_average(self) -> None:
        setup = self.latest_base()
        too_small = candle(100, 100.7, 99.9, 100.2, setup.reference_volume * 3 - 0.01, self.now_ms)
        exact = candle(
            100,
            100.7,
            99.9,
            100.2,
            setup.reference_volume * main.TRIGGER_VOLUME_MULTIPLE,
            self.now_ms,
        )

        rejected = evaluate_trigger("THRESHOLDUSDT", setup, too_small, self.now_ms)
        accepted = evaluate_trigger("THRESHOLDUSDT", setup, exact, self.now_ms)

        self.assertIsNone(rejected.signal)
        self.assertEqual(rejected.rejection_reason, "volume_spike_too_small")
        self.assertIsNotNone(accepted.signal)
        self.assertAlmostEqual(accepted.signal.volume_ratio, main.TRIGGER_VOLUME_MULTIPLE)

    def test_missed_scan_recovery_alerts_when_next_scan_price_still_beyond_original_high(self) -> None:
        setup = self.latest_base()
        later_latest = candle(100.7, 100.9, 100.55, 100.58, setup.reference_volume * 3.2, self.now_ms + 60_000)
        signal = evaluate_trigger("RECOVERYUSDT", setup, later_latest, self.now_ms + 60_000).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.setup.base_high, 100.45)

    def test_later_candles_do_not_replace_original_base_values_inside_existing_setup(self) -> None:
        setup = self.latest_base()
        later_latest = candle(104, 105, 103, 104.5, setup.reference_volume * 4, self.now_ms)
        signal = evaluate_trigger("ORIGINALUSDT", setup, later_latest, self.now_ms).signal
        self.assertIsNotNone(signal)
        self.assertEqual(signal.setup.base_high, 100.45)
        self.assertEqual(signal.setup.base_low, 99.75)
        self.assertAlmostEqual(signal.setup.reference_volume, 50.5)

    def test_detect_signal_uses_15m_base_and_latest_15m_candle_only(self) -> None:
        trigger = [candle(100, 100.2, 99.9, 100.1, 10, self.now_ms - 300_000), candle(100, 100.7, 99.9, 100.3, 200, self.now_ms)]
        with patch("main.time.time", return_value=self.now_ms / 1000):
            signal = detect_signal("FASTUSDT", self.base_candles(), trigger)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.candle.timestamp, self.now_ms)

    def test_live_trigger_candle_is_after_fully_closed_base(self) -> None:
        setup = self.latest_base()
        closed_base_candles = self.base_candles()
        forming_trigger = candle(100, 100.7, 99.9, 100.3, setup.reference_volume * 3, self.now_ms)

        trigger = get_latest_trigger_candle(closed_base_candles + [forming_trigger], setup)
        signal = evaluate_trigger("LIVEUSDT", setup, trigger, self.now_ms).signal if trigger else None

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.timestamp, self.now_ms)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")

    def test_last_base_candle_is_not_reused_as_trigger_when_no_live_candle_exists(self) -> None:
        setup = self.latest_base()

        trigger = get_latest_trigger_candle(self.base_candles(), setup)

        self.assertIsNone(trigger)

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

    def test_setup_remains_active_without_time_expiry(self) -> None:
        setup = self.latest_base()
        latest = candle(100, 101, 99, 100.8, setup.reference_volume * 4, setup.expires_at + 1)
        evaluation = evaluate_trigger("EXPIREUSDT", setup, latest, setup.expires_at + 1)
        self.assertIsNotNone(evaluation.signal)


if __name__ == "__main__":
    unittest.main()
