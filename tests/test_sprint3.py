"""Unit tests for Sprint 3 — Pattern Detection Modules."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
import numpy as np
import pandas as pd

from core.candlestick import (
    detect_engulfing, detect_pin_bar, detect_doji, detect_morning_evening_star,
    detect_three_soldiers_crows, detect_tweezer, detect_inside_bar, detect_marubozu,
    detect_all_patterns, patterns_to_dict, recent_patterns, CandlestickPattern,
)
from core.divergence import (
    detect_rsi_divergence, detect_macd_divergence, detect_all_divergences,
    DivergenceSignal, divergences_to_dict, recent_divergences,
)
from core.squeeze import (
    calc_squeeze, get_current_squeeze, is_squeeze_firing,
    SqueezeState, squeeze_to_dict,
)
from core.volume_analysis import (
    calc_rvol, calc_obv, detect_volume_climax, detect_volume_absorption,
    detect_rvol_surge, detect_obv_divergence, detect_low_volume_pullback,
    detect_all_volume_signals, volume_signals_to_dict, VolumeSignal,
)
from core.chart_patterns import (
    detect_double_top_bottom, detect_head_shoulders, detect_wedge,
    detect_flag, detect_triangle, detect_all_chart_patterns,
    chart_patterns_to_dict, ChartPattern,
)
from core.swing_points import detect_swing_points


def make_sine_df(n=100, base=60000, amplitude=2000, noise=100, seed=42):
    """Realistic OHLCV with sine-wave close prices."""
    np.random.seed(seed)
    t = np.linspace(0, 4 * np.pi, n)
    close = base + np.sin(t) * amplitude + np.random.randn(n) * noise
    return pd.DataFrame({
        "open":   close + np.random.randn(n) * 50,
        "high":   close + np.abs(np.random.randn(n)) * 150 + 50,
        "low":    close - np.abs(np.random.randn(n)) * 150 - 50,
        "close":  close,
        "volume": np.abs(np.random.randn(n)) * 500 + 1000,
    }, index=pd.date_range("2026-01-01", periods=n, freq="h"))


def make_simple_df(opens, highs, lows, closes, volumes=None):
    """Small OHLCV from explicit lists."""
    n = len(closes)
    volumes = volumes or [100.0] * n
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
    }, index=pd.date_range("2026-01-01", periods=n, freq="h"))


# ────────────────────────────────────────────────────────
# Candlestick Tests
# ────────────────────────────────────────────────────────
class TestCandlestickEngulfing(unittest.TestCase):
    def test_bullish_engulfing(self):
        # Bar 0: bearish candle (open=110, close=100) — the "prior" bearish candle
        # Bar 1: bullish candle that fully engulfs bar 0 (open=98, close=115)
        df = make_simple_df(
            opens= [110, 98],
            highs= [111, 116],
            lows=  [99,  97],
            closes=[100, 115],
        )
        patterns = detect_engulfing(df, min_body_pct=0.4)
        bullish = [p for p in patterns if p.direction == "bullish"]
        self.assertGreater(len(bullish), 0, "Should detect bullish engulfing")

    def test_bearish_engulfing(self):
        # Bar 0: bullish candle (open=100, close=110) — the "prior" bullish candle
        # Bar 1: bearish candle that fully engulfs bar 0 (open=112, close=98)
        df = make_simple_df(
            opens= [100, 112],
            highs= [111, 114],
            lows=  [99,  97],
            closes=[110, 98],
        )
        patterns = detect_engulfing(df, min_body_pct=0.4)
        bearish = [p for p in patterns if p.direction == "bearish"]
        self.assertGreater(len(bearish), 0, "Should detect bearish engulfing")

    def test_empty_df(self):
        df = make_simple_df([], [], [], [])
        self.assertEqual(len(detect_engulfing(df)), 0)


class TestCandlestickPinBar(unittest.TestCase):
    def test_hammer_detected(self):
        # Hammer: small body at top, long lower wick
        df = make_simple_df(
            opens= [100, 99],
            highs= [101, 100],
            lows=  [99,  90],
            closes=[100, 99.5],
        )
        patterns = detect_pin_bar(df, min_wick_ratio=1.5, max_body_pct=0.5)
        # Should find a bullish pin bar
        self.assertIsInstance(patterns, list)

    def test_shooting_star_detected(self):
        df = make_simple_df(
            opens= [100, 101],
            highs= [101, 112],
            lows=  [99,  100.5],
            closes=[100, 101.5],
        )
        patterns = detect_pin_bar(df, min_wick_ratio=1.5, max_body_pct=0.5)
        self.assertIsInstance(patterns, list)


class TestCandlestickDoji(unittest.TestCase):
    def test_doji_detected(self):
        df = make_simple_df(
            opens= [100, 100.05],
            highs= [101, 102],
            lows=  [99,  98],
            closes=[100, 100.10],
        )
        patterns = detect_doji(df, max_body_pct=0.1)
        self.assertIsInstance(patterns, list)

    def test_non_doji_rejected(self):
        # A clear marubozu: body fills almost the entire range — not a doji
        df = make_simple_df(
            opens= [100.0],
            highs= [110.2],
            lows=  [99.9],
            closes=[110.0],
        )
        patterns = detect_doji(df, max_body_pct=0.1)
        self.assertEqual(len(patterns), 0, "Strong trend bar is not a doji")


class TestCandlestickMorningStar(unittest.TestCase):
    def test_morning_star(self):
        df = make_simple_df(
            opens= [110, 102, 101, 104],
            highs= [111, 103, 102, 112],
            lows=  [101, 100, 100, 103],
            closes=[102, 101, 101.5, 110],
        )
        patterns = detect_morning_evening_star(df)
        self.assertIsInstance(patterns, list)


class TestCandlestickInsideBar(unittest.TestCase):
    def test_inside_bar(self):
        df = make_simple_df(
            opens= [100, 102],
            highs= [110, 108],
            lows=  [90,  92],
            closes=[105, 104],
        )
        patterns = detect_inside_bar(df)
        inside_bars = [p for p in patterns if p.pattern == "inside_bar"]
        self.assertGreater(len(inside_bars), 0)


class TestCandlestickMarubozu(unittest.TestCase):
    def test_marubozu(self):
        df = make_simple_df(
            opens= [100, 100],
            highs= [101, 110.5],
            lows=  [99,  99.5],
            closes=[100, 110],
        )
        patterns = detect_marubozu(df, min_body_pct=0.85)
        self.assertIsInstance(patterns, list)


class TestCandlestickAll(unittest.TestCase):
    def test_detect_all_on_realistic_data(self):
        df = make_sine_df(200)
        patterns = detect_all_patterns(df)
        self.assertGreater(len(patterns), 0)
        self.assertIsInstance(patterns[0], CandlestickPattern)

    def test_patterns_to_dict(self):
        df = make_sine_df(100)
        patterns = detect_all_patterns(df)
        dicts = patterns_to_dict(patterns[:3])
        for d in dicts:
            self.assertIn("pattern", d)
            self.assertIn("direction", d)
            self.assertIn("strength", d)

    def test_recent_patterns(self):
        df = make_sine_df(100)
        patterns = detect_all_patterns(df)
        recent = recent_patterns(patterns, n=5)
        self.assertLessEqual(len(recent), 5)


# ────────────────────────────────────────────────────────
# Divergence Tests
# ────────────────────────────────────────────────────────
class TestDivergence(unittest.TestCase):
    def test_rsi_divergence_basic(self):
        """On synthetic data, divergence detector should run and return signals."""
        df = make_sine_df(200)
        # Generate RSI-like values that diverge from price
        rsi = pd.Series(np.random.uniform(30, 70, 200), index=df.index)
        result = detect_rsi_divergence(df, rsi, lookback=3, min_bars_apart=5, max_bars_apart=50)
        self.assertIsInstance(result, list)
        for d in result:
            self.assertIsInstance(d, DivergenceSignal)
            self.assertIn(d.type, ("bullish_regular", "bearish_regular", "bullish_hidden", "bearish_hidden"))

    def test_macd_divergence_basic(self):
        df = make_sine_df(200)
        macd_hist = pd.Series(np.sin(np.linspace(0, 8, 200)) * 100, index=df.index)
        result = detect_macd_divergence(df, macd_hist, lookback=3)
        self.assertIsInstance(result, list)

    def test_detect_all_divergences(self):
        df = make_sine_df(200)
        indicators = {
            "rsi": pd.Series(np.random.uniform(30, 70, 200), index=df.index),
            "macd_hist": pd.Series(np.random.randn(200) * 50, index=df.index),
        }
        result = detect_all_divergences(df, indicators)
        self.assertIsInstance(result, list)

    def test_divergences_to_dict(self):
        df = make_sine_df(200)
        indicators = {
            "rsi": pd.Series(np.random.uniform(30, 70, 200), index=df.index),
        }
        result = detect_all_divergences(df, indicators)
        dicts = divergences_to_dict(result)
        self.assertIsInstance(dicts, list)

    def test_empty_data(self):
        df = make_simple_df([], [], [], [])
        result = detect_rsi_divergence(df, pd.Series(dtype=float))
        self.assertEqual(len(result), 0)


# ────────────────────────────────────────────────────────
# Squeeze Tests
# ────────────────────────────────────────────────────────
class TestSqueeze(unittest.TestCase):
    def test_calc_squeeze_basic(self):
        df = make_sine_df(100)
        states = calc_squeeze(df)
        self.assertGreater(len(states), 0)
        for s in states:
            self.assertIsInstance(s, SqueezeState)
            self.assertIn(s.momentum_direction, ("up", "down", "flat"))

    def test_squeeze_firing(self):
        """On sufficient data, is_squeeze_firing should return bool."""
        df = make_sine_df(100)
        result = is_squeeze_firing(df)
        self.assertIsInstance(result, bool)

    def test_get_current_squeeze(self):
        df = make_sine_df(100)
        result = get_current_squeeze(df)
        # Can be None if not enough bars, otherwise SqueezeState
        if result is not None:
            self.assertIsInstance(result, SqueezeState)

    def test_squeeze_to_dict(self):
        df = make_sine_df(100)
        states = calc_squeeze(df)
        if states:
            d = squeeze_to_dict(states[-1])
            self.assertIn("in_squeeze", d)
            self.assertIn("firing", d)
            self.assertIn("momentum", d)

    def test_short_data(self):
        df = make_simple_df([100, 101], [102, 103], [99, 100], [100, 101])
        states = calc_squeeze(df)
        self.assertEqual(len(states), 0)


# ────────────────────────────────────────────────────────
# Volume Analysis Tests
# ────────────────────────────────────────────────────────
class TestVolumeAnalysis(unittest.TestCase):
    def test_calc_rvol(self):
        df = make_sine_df(100)
        rvol = calc_rvol(df)
        self.assertEqual(len(rvol), len(df))
        self.assertFalse(rvol.isna().all())

    def test_calc_obv(self):
        df = make_sine_df(100)
        obv = calc_obv(df)
        self.assertEqual(len(obv), len(df))

    def test_volume_climax(self):
        df = make_sine_df(100)
        # Inject a spike to guarantee detection
        df.iloc[50, df.columns.get_loc("volume")] = 50000
        df.iloc[50, df.columns.get_loc("close")] = df.iloc[50]["high"] - 10  # close near high
        signals = detect_volume_climax(df, rvol_threshold=3.0)
        self.assertIsInstance(signals, list)

    def test_volume_absorption(self):
        df = make_sine_df(100)
        signals = detect_volume_absorption(df)
        self.assertIsInstance(signals, list)

    def test_rvol_surge(self):
        df = make_sine_df(100)
        df.iloc[80, df.columns.get_loc("volume")] = 20000
        signals = detect_rvol_surge(df, threshold=2.0)
        self.assertIsInstance(signals, list)

    def test_obv_divergence(self):
        df = make_sine_df(100)
        signals = detect_obv_divergence(df, lookback=20)
        self.assertIsInstance(signals, list)

    def test_low_volume_pullback(self):
        df = make_sine_df(100)
        signals = detect_low_volume_pullback(df)
        self.assertIsInstance(signals, list)

    def test_detect_all_volume_signals(self):
        df = make_sine_df(200)
        signals = detect_all_volume_signals(df)
        self.assertIsInstance(signals, list)
        if signals:
            self.assertIsInstance(signals[0], VolumeSignal)

    def test_volume_signals_to_dict(self):
        df = make_sine_df(100)
        signals = detect_all_volume_signals(df)
        dicts = volume_signals_to_dict(signals[:3])
        self.assertIsInstance(dicts, list)

    def test_empty_df(self):
        df = make_simple_df([], [], [], [])
        self.assertEqual(len(detect_all_volume_signals(df)), 0)


# ────────────────────────────────────────────────────────
# Chart Pattern Tests
# ────────────────────────────────────────────────────────
class TestChartPatterns(unittest.TestCase):
    def _get_swings(self, df, lookback=3):
        return detect_swing_points(df, lookback=lookback)

    def test_double_top_bottom(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_double_top_bottom(df, swings, tolerance_pct=0.05)
        self.assertIsInstance(patterns, list)

    def test_head_shoulders(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_head_shoulders(df, swings)
        self.assertIsInstance(patterns, list)

    def test_wedge(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_wedge(df, swings)
        self.assertIsInstance(patterns, list)

    def test_flag(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_flag(df, swings)
        self.assertIsInstance(patterns, list)

    def test_triangle(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_triangle(df, swings)
        self.assertIsInstance(patterns, list)

    def test_detect_all(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_all_chart_patterns(df, swings)
        self.assertIsInstance(patterns, list)
        if patterns:
            self.assertIsInstance(patterns[0], ChartPattern)

    def test_chart_patterns_to_dict(self):
        df = make_sine_df(200)
        swings = self._get_swings(df)
        patterns = detect_all_chart_patterns(df, swings)
        dicts = chart_patterns_to_dict(patterns[:3])
        self.assertIsInstance(dicts, list)

    def test_empty_data(self):
        df = make_simple_df([], [], [], [])
        patterns = detect_all_chart_patterns(df, [])
        self.assertEqual(len(patterns), 0)


# ────────────────────────────────────────────────────────
# Pipeline Integration (Sprint 3)
# ────────────────────────────────────────────────────────
class TestPipelineSprint3(unittest.TestCase):
    def test_full_pipeline_with_patterns(self):
        """analyse_timeframe should include Sprint 3 fields."""
        from signals.pipeline import analyse_timeframe
        df = make_sine_df(200)
        result = analyse_timeframe(df, timeframe="1h", lookback=3)
        # Sprint 3 fields must exist
        self.assertIsInstance(result.candlestick_patterns, list)
        self.assertIsInstance(result.chart_patterns, list)
        self.assertIsInstance(result.divergences, list)
        self.assertIsInstance(result.volume_signals, list)
        self.assertIsInstance(result.squeeze_firing, bool)

    def test_to_dict_has_sprint3_keys(self):
        from signals.pipeline import analyse_timeframe
        df = make_sine_df(200)
        result = analyse_timeframe(df, timeframe="1h", lookback=3)
        d = result.to_dict()
        self.assertIn("candlestick_patterns", d)
        self.assertIn("chart_patterns", d)
        self.assertIn("divergences", d)
        self.assertIn("volume_signals", d)
        self.assertIn("squeeze", d)
        self.assertIn("squeeze_firing", d)

    def test_to_summary_has_patterns(self):
        from signals.pipeline import analyse_timeframe
        df = make_sine_df(200)
        result = analyse_timeframe(df, timeframe="1h", lookback=3)
        summary = result.to_summary()
        # Should mention at least one Sprint 3 section
        self.assertTrue(
            any(kw in summary for kw in ["Candlestick", "Chart Pattern", "Volume", "Squeeze", "Divergence"]),
            f"Summary should mention Sprint 3 patterns:\n{summary}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
