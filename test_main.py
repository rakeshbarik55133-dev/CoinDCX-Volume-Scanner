import unittest

from main import Candle, get_candles, get_usdt_pairs, detect_signal, evaluate_signal


def candle(
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    timestamp: int,
) -> Candle:
    return Candle(
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timestamp=timestamp,
    )


class CoinDCXCandleFetchTests(unittest.TestCase):
    def test_get_usdt_pairs_uses_market_details_pair_for_candle_api(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict[str, str]]:
                return [
                    {
                        "coindcx_name": "BTCUSDT",
                        "base_currency_short_name": "USDT",
                        "pair": "B-BTC_USDT",
                        "status": "active",
                    },
                    {
                        "coindcx_name": "ETHBTC",
                        "base_currency_short_name": "BTC",
                        "pair": "B-ETH_BTC",
                        "status": "active",
                    },
                ]

        class Session:
            def get(self, *args, **kwargs) -> Response:
                return Response()

        self.assertEqual(get_usdt_pairs(Session()), ["B-BTC_USDT"])

    def test_get_candles_parses_descending_api_candles_chronologically(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            status_code = 200
            text = "[]"

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
        candles = get_candles(session, "B-BTC_USDT")

        self.assertEqual(session.params["pair"], "B-BTC_USDT")
        self.assertEqual([item.timestamp for item in candles], [1000, 2000])
        self.assertEqual(candles[0].close, 1.5)

    def test_get_candles_parses_array_candles_chronologically(self) -> None:
        class Response:
            status_code = 200
            text = "[]"

            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[list[object]]:
                return [
                    [2000, "2", "3", "1", "2.5", "10"],
                    [1000, "1", "2", "0.5", "1.5", "8"],
                ]

        class Session:
            def get(self, _url, params, timeout) -> Response:
                return Response()

        candles = get_candles(Session(), "B-BTC_USDT")

        self.assertEqual([item.timestamp for item in candles], [1000, 2000])
        self.assertEqual(candles[1].volume, 10)

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
            get_candles(Session(), "B-BTC_USDT", log_response=True)

        self.assertIn("status=200", logs.output[0])
        self.assertIn(Response.text, logs.output[0])


class DeadVolumeSpikeBreakoutTests(unittest.TestCase):
    def base_candles(self) -> list[Candle]:
        candles: list[Candle] = []
        timestamp = 1_700_000_000_000
        for index in range(12):
            price = 100 + (index % 3) * 0.2
            candles.append(candle(price, price + 0.4, price - 0.4, price + 0.1, 100, timestamp))
            timestamp += 900_000
        for index in range(12):
            price = 100 + (index % 2) * 0.1
            candles.append(candle(price, 100.45, 99.7, price + 0.02, 55, timestamp))
            timestamp += 900_000
        return candles

    def test_detects_bullish_dead_volume_spike_breakout(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 190, 1_700_021_600_000))

        signal = detect_signal("BLURUSDT", candles)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BREAKOUT")
        self.assertGreaterEqual(signal.spike_ratio, 2.2)

    def test_detects_bearish_dead_volume_spike_breakdown(self) -> None:
        candles = self.base_candles()
        candles.append(candle(99.8, 100.0, 96.6, 97.1, 190, 1_700_021_600_000))

        signal = detect_signal("MUSDT", candles)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BREAKDOWN")
        self.assertGreaterEqual(signal.spike_ratio, 2.2)

    def test_rejects_late_overextended_move(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.3, 116.0, 100.2, 115.0, 220, 1_700_021_600_000))
        candles.append(candle(115.0, 115.2, 114.0, 114.5, 80, 1_700_022_500_000))

        self.assertIsNone(detect_signal("YFIUSDT", candles))

    def test_rejects_choppy_non_flat_range(self) -> None:
        candles = self.base_candles()
        choppy_start = len(candles) - 12
        for index in range(choppy_start, len(candles)):
            candles[index] = candle(100, 106, 98, 101, 55, candles[index].timestamp)
        candles.append(candle(101, 108, 100.5, 107.5, 220, 1_700_021_600_000))
        candles.append(candle(107.5, 108, 107, 107.2, 80, 1_700_022_500_000))

        self.assertIsNone(detect_signal("BUBBLEUSDT", candles))

    def test_rejects_legacy_breakout_without_dead_volume(self) -> None:
        candles = self.base_candles()
        for index in range(len(candles) - 8, len(candles)):
            candles[index] = candle(100, 100.45, 99.7, 100.02, 95, candles[index].timestamp)
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 220, 1_700_021_600_000))
        candles.append(candle(102.8, 102.9, 102.1, 102.4, 80, 1_700_022_500_000))

        self.assertIsNone(detect_signal("OLDLOGICUSDT", candles))

    def test_rejects_legacy_wick_break_even_with_volume_spike(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.4, 105.0, 100.2, 100.7, 220, 1_700_021_600_000))
        candles.append(candle(100.7, 100.9, 100.1, 100.4, 80, 1_700_022_500_000))

        self.assertIsNone(detect_signal("WICKUSDT", candles))

    def test_alerts_only_on_breakout_candle(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 190, 1_700_021_600_000))

        signal = detect_signal("WAITUSDT", candles)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.confirmation_candle.timestamp, 1_700_021_600_000)

    def test_rejects_setup_without_trigger_high_break(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 190, 1_700_021_600_000))
        candles.append(candle(102.8, 103.2, 102.0, 103.1, 80, 1_700_022_500_000))
        candles.append(candle(103.1, 103.3, 102.7, 103.2, 80, 1_700_023_400_000))
        candles.append(candle(103.2, 103.3, 102.8, 103.1, 80, 1_700_024_300_000))
        candles.append(candle(103.1, 103.2, 102.7, 103.0, 80, 1_700_025_200_000))
        candles.append(candle(103.0, 103.3, 102.6, 103.2, 80, 1_700_026_100_000))
        candles.append(candle(103.2, 103.3, 102.9, 103.1, 80, 1_700_027_000_000))

        self.assertIsNone(detect_signal("INVALIDUSDT", candles))

    def test_ignores_break_after_fifth_confirmation_candle(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 190, 1_700_021_600_000))
        candles.append(candle(102.8, 103.2, 102.2, 103.1, 80, 1_700_022_500_000))
        candles.append(candle(103.1, 103.3, 102.7, 103.2, 80, 1_700_023_400_000))
        candles.append(candle(103.2, 103.3, 102.8, 103.1, 80, 1_700_024_300_000))
        candles.append(candle(103.1, 103.2, 102.7, 103.0, 80, 1_700_025_200_000))
        candles.append(candle(103.0, 103.3, 102.6, 103.2, 80, 1_700_026_100_000))
        candles.append(candle(103.2, 103.8, 102.9, 103.6, 80, 1_700_027_000_000))
        candles.append(candle(103.6, 103.9, 103.1, 103.5, 80, 1_700_027_900_000))

        self.assertIsNone(detect_signal("LATEUSDT", candles))

    def test_reports_rejection_reason_for_missing_volume_spike(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 90, 1_700_021_600_000))

        evaluation = evaluate_signal("NOSPIKEUSDT", candles)

        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "volume_spike_too_small")


if __name__ == "__main__":
    unittest.main()
