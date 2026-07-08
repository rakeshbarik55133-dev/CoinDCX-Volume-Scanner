import unittest

from main import Candle, detect_signal, evaluate_signal


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
        candles.append(candle(102.8, 103.8, 102.2, 103.6, 80, 1_700_022_500_000))

        signal = detect_signal("BLURUSDT", candles)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BREAKOUT")
        self.assertGreaterEqual(signal.spike_ratio, 2.2)

    def test_detects_bearish_dead_volume_spike_breakdown(self) -> None:
        candles = self.base_candles()
        candles.append(candle(99.8, 100.0, 96.6, 97.1, 190, 1_700_021_600_000))
        candles.append(candle(97.1, 97.3, 96.1, 96.4, 80, 1_700_022_500_000))

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

    def test_alerts_on_latest_closed_breakout_candle(self) -> None:
        candles = self.base_candles()
        candles.append(candle(100.35, 103.4, 100.2, 102.8, 190, 1_700_021_600_000))
        candles.append(candle(102.8, 102.9, 102.1, 102.4, 80, 1_700_022_500_000))

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
        candles.append(candle(102.8, 103.8, 102.2, 103.6, 80, 1_700_022_500_000))

        evaluation = evaluate_signal("NOSPIKEUSDT", candles)

        self.assertIsNone(evaluation.signal)
        self.assertEqual(evaluation.rejection_reason, "volume_spike_too_small")


if __name__ == "__main__":
    unittest.main()
