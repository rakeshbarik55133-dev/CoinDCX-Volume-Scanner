import unittest

from main import Candle, detect_signal


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
        candles.append(candle(102.8, 102.9, 102.1, 102.4, 80, 1_700_022_500_000))

        signal = detect_signal("BLURUSDT", candles)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BREAKOUT")
        self.assertGreaterEqual(signal.spike_ratio, 2.2)

    def test_detects_bearish_dead_volume_spike_breakdown(self) -> None:
        candles = self.base_candles()
        candles.append(candle(99.8, 100.0, 96.6, 97.1, 190, 1_700_021_600_000))
        candles.append(candle(97.1, 97.8, 96.9, 97.4, 80, 1_700_022_500_000))

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


if __name__ == "__main__":
    unittest.main()
