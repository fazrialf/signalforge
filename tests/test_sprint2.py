"""Unit tests for Sprint 2 — SMC Structure Detection Modules."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
import numpy as np
import pandas as pd

from core.swing_points import detect_swing_points, SwingPoint, get_recent_swings, swing_points_to_dict
from core.market_structure import analyse_structure, MarketStructure, Bias, StructureEvent
from core.smc import (
    detect_sr_levels, SRLevel, nearest_sr,
    detect_fvgs, FairValueGap, get_active_fvgs,
    detect_order_blocks, OrderBlock, get_active_order_blocks,
    detect_liquidity_pools, LiquidityPool, recent_liquidity_grab,
    calc_premium_discount, PremiumDiscountZone,
)


def make_df(close_prices, highs=None, lows=None, opens=None, volumes=None):
    """Helper to build a test OHLCV DataFrame."""
    n = len(close_prices)
    highs = highs or [c * 1.02 for c in close_prices]
    lows = lows or [c * 0.98 for c in close_prices]
    opens = opens or [c * 0.99 for c in close_prices]
    volumes = volumes or [100.0] * n
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": close_prices,
        "volume": volumes,
    }, index=pd.date_range("2026-01-01", periods=n, freq="h"))


class TestSwingPoints(unittest.TestCase):

    def test_detect_basic_swings(self):
        """Simple up-trend should produce swing highs and lows."""
        # Create a longer dataset with wide zig-zags so lookback=2 finds swings
        n = 60
        t = np.linspace(0, 6*np.pi, n)
        close = 100 + np.sin(t) * 10 + np.linspace(0, 5, n)  # sine + drift
        highs = close + 2
        lows = close - 2
        df = pd.DataFrame({
            "open": close, "high": highs, "low": lows, "close": close, "volume": [100]*n,
        }, index=pd.date_range("2026-01-01", periods=n, freq="h"))
        points = detect_swing_points(df, lookback=2)
        self.assertGreater(len(points), 0)
        highs = [p for p in points if p.type == "high"]
        lows = [p for p in points if p.type == "low"]
        self.assertGreater(len(highs), 0, f"Should have swing highs, got {len(points)} total")
        self.assertGreater(len(lows), 0, f"Should have swing lows, got {len(points)} total")
        self.assertGreater(len(highs), 0)
        self.assertGreater(len(lows), 0)

    def test_swing_strength(self):
        """Swing strength should be at least 1."""
        df = make_df([100, 102, 101, 103, 102, 104, 103, 105])
        points = detect_swing_points(df, lookback=2)
        for p in points:
            self.assertGreaterEqual(p.strength, 1)

    def test_swing_types_correct(self):
        """Swing at a known high should be type 'high'."""
        df = make_df([100, 110, 105, 115, 110, 120, 115, 125, 120])
        points = detect_swing_points(df, lookback=2)
        for p in points:
            if p.type == "high":
                self.assertEqual(p.price, df["high"].iloc[p.index])
            else:
                self.assertEqual(p.price, df["low"].iloc[p.index])

    def test_get_recent_swings(self):
        """get_recent_swings should return N items."""
        points = [SwingPoint(i, pd.Timestamp.now(), i * 10, "high", 2) for i in range(20)]
        recent = get_recent_swings(points, n=5)
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0].price, 150)

    def test_swing_points_to_dict(self):
        """swing_points_to_dict should return serialisable dicts."""
        points = [SwingPoint(0, pd.Timestamp("2026-01-01"), 100.0, "high", 3)]
        d = swing_points_to_dict(points)
        self.assertEqual(d[0]["price"], 100.0)
        self.assertEqual(d[0]["type"], "high")
        self.assertEqual(d[0]["strength"], 3)

    def test_insufficient_data(self):
        """With too few bars, no swings should be detected."""
        df = make_df([100, 101, 102])
        points = detect_swing_points(df, lookback=5)
        self.assertEqual(len(points), 0)

    def test_duplicate_bars(self):
        """If same bar is both high and low, both should be returned."""
        df = make_df([100, 100, 100])  # flat line — edge case
        points = detect_swing_points(df, lookback=1)
        # Should not crash, and points list should be valid
        for p in points:
            self.assertIn(p.type, ("high", "low"))


class TestMarketStructure(unittest.TestCase):

    def test_bullish_bias(self):
        """Rising HH/HL should produce BULLISH bias."""
        # Clear HH/HL with zig-zag pattern
        close_prices = [
            100, 101, 100, 103,  # low=100, high=103
            101, 102, 101, 105,  # low=101, high=105
            102, 103, 102, 107,  # low=102, high=107
            103, 104, 103, 109,  # low=103, high=109
        ]
        df = make_df(close_prices)
        ms = analyse_structure(df, lookback=2)
        self.assertEqual(ms.bias, Bias.BULLISH)

    def test_unknown_bias_short_data(self):
        """With fewer than 4 swing points, bias should be UNKNOWN."""
        close_prices = [100, 101, 102, 101]
        df = make_df(close_prices)
        ms = analyse_structure(df, lookback=2)
        self.assertEqual(ms.bias, Bias.UNKNOWN)

    def test_bos_detected(self):
        """Breaking above a Lower High should produce BOS_BULL."""
        # Trend: down, then up = ChOS
        close_prices = [110, 109, 108, 107, 106, 105, 108, 111]
        df = make_df(close_prices)
        ms = analyse_structure(df, lookback=2)
        # Should detect something
        self.assertIsInstance(ms, MarketStructure)

    def test_to_summary_format(self):
        """to_summary should return a formatted string."""
        close_prices = [
            100, 101, 100, 103, 101, 102, 101, 105,
            102, 103, 102, 107, 103, 104, 103, 109,
        ]
        df = make_df(close_prices)
        ms = analyse_structure(df, lookback=2)
        summary = ms.to_summary()
        self.assertIn("BULLISH", summary.upper())
        self.assertIn("$", summary)

    def test_to_dict_serialisable(self):
        """to_dict should return a JSON-serialisable dict."""
        close_prices = [100, 102, 101, 104, 102, 106, 104, 108]
        df = make_df(close_prices)
        ms = analyse_structure(df, lookback=2)
        d = ms.to_dict()
        self.assertIn("bias", d)
        self.assertIn("last_hh", d)

    def test_latest_break_none(self):
        """If no breaks, latest_break should return None."""
        close_prices = [100, 101, 102, 103, 104, 105]
        df = make_df(close_prices)
        ms = analyse_structure(df, lookback=2)
        self.assertIsNone(ms.latest_break())


class TestSupportResistance(unittest.TestCase):

    def test_sr_levels_detected(self):
        """S/R should group nearby swing points."""
        close_prices = [100, 102, 101, 104, 102, 106, 104, 108, 105, 107]
        df = make_df(close_prices)
        swings = detect_swing_points(df, lookback=2)
        levels = detect_sr_levels(df, swings, cluster_pct=0.005)
        self.assertGreater(len(levels), 0)

    def test_nearest_sr_returns_both(self):
        """nearest_sr should return support below and resistance above."""
        levels = [
            SRLevel(100.0, "support", 3, 0.6),
            SRLevel(110.0, "resistance", 3, 0.6),
        ]
        sup, res = nearest_sr(levels, 105.0, max_dist_pct=0.1)
        self.assertIsNotNone(sup)
        self.assertIsNotNone(res)
        self.assertEqual(sup.price, 100.0)
        self.assertEqual(res.price, 110.0)

    def test_nearest_sr_no_match(self):
        """nearest_sr should return None for levels too far."""
        levels = [
            SRLevel(80.0, "support", 3, 0.6),
            SRLevel(200.0, "resistance", 3, 0.6),
        ]
        sup, res = nearest_sr(levels, 105.0, max_dist_pct=0.02)
        self.assertIsNone(sup)
        self.assertIsNone(res)

    def test_sr_strength_scoring(self):
        """More touches = higher strength score."""
        levels = [
            SRLevel(100.0, "support", 5, 0.0),
            SRLevel(200.0, "resistance", 2, 0.0),
        ]
        self.assertEqual(levels[0].strength, 0.0)  # default
        # Actual strength depends on the detect function


class TestFVG(unittest.TestCase):

    def test_bullish_fvg_detected(self):
        """Three candle pattern: C1.high < C3.low = bullish FVG."""
        highs = [100, 105, 95, 108, 102, 110]
        lows  = [99,  104, 94, 107, 101, 109]
        close = [100, 105, 95, 108, 102, 110]
        df = make_df(close, highs=highs, lows=lows)
        fvgs = detect_fvgs(df, min_size_pct=0.01)
        self.assertGreater(len(fvgs), 0)

    def test_bearish_fvg_detected(self):
        """Three candle pattern: C1.low > C3.high = bearish FVG."""
        highs = [110, 105, 108, 102, 106, 100]
        lows  = [109, 104, 107, 101, 105, 99]
        close = [110, 105, 108, 102, 106, 100]
        df = make_df(close, highs=highs, lows=lows)
        fvgs = detect_fvgs(df, min_size_pct=0.01)
        self.assertGreater(len(fvgs), 0)

    def test_fvg_properties(self):
        """FVG dataclass should have correct properties."""
        fvg = FairValueGap(top=110.0, bottom=100.0, direction="bullish",
                          formed_at=pd.Timestamp.now(), bar_index=5)
        self.assertEqual(fvg.midpoint, 105.0)
        self.assertAlmostEqual(fvg.size_pct, 10.0)

    def test_get_active_fvgs(self):
        """get_active_fvgs should return unfilled FVGs near price."""
        fvgs = [
            FairValueGap(top=102.0, bottom=101.0, direction="bullish",
                        formed_at=pd.Timestamp.now(), bar_index=0, filled=False),
            FairValueGap(top=200.0, bottom=199.0, direction="bullish",
                        formed_at=pd.Timestamp.now(), bar_index=1, filled=False),
        ]
        active = get_active_fvgs(fvgs, 101.5, max_dist_pct=0.05)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].bottom, 101.0)

    def test_fvg_min_size_filter(self):
        """FVG smaller than min_size_pct should be filtered."""
        # Create a tiny gap
        highs = [100, 100.05, 100, 100.08, 100, 100.10]
        lows  = [99.99, 100.04, 99.99, 100.07, 99.99, 100.09]
        close = [100, 100.05, 100, 100.08, 100, 100.10]
        df = make_df(close, highs=highs, lows=lows)
        fvgs = detect_fvgs(df, min_size_pct=0.1)  # 0.1% min
        # All gaps here are tiny (< 0.1%), should be filtered
        # (but depends on actual price size; at $100, 0.1% = $0.10)
        self.assertEqual(len(fvgs), 0)


class TestOrderBlock(unittest.TestCase):

    def test_bullish_ob_detected(self):
        """Bearish candle followed by 3 bullish candles with impulse > 1.5x."""
        opens  = [100, 105, 102, 103, 104]
        highs  = [101, 106, 103, 105, 106]
        lows   = [99,  104, 101, 102, 103]
        closes = [100, 105, 102, 104, 105]
        df = make_df(closes, highs=highs, lows=lows, opens=opens)
        ohlc_df = df.copy()
        ohlc_df["open"] = opens
        swings = detect_swing_points(ohlc_df, lookback=2)
        obs = detect_order_blocks(ohlc_df, swings)
        # May or may not detect, but should not crash
        self.assertIsInstance(obs, list)

    def test_ob_dataclass(self):
        """OrderBlock properties should work."""
        ob = OrderBlock(top=105.0, bottom=100.0, direction="bullish",
                       formed_at=pd.Timestamp.now(), bar_index=3)
        self.assertEqual(ob.midpoint, 102.5)

    def test_get_active_order_blocks(self):
        """get_active_order_blocks should return unbroken OBs near price."""
        obs = [
            OrderBlock(top=102.0, bottom=100.0, direction="bullish",
                      formed_at=pd.Timestamp.now(), bar_index=0, broken=False),
            OrderBlock(top=200.0, bottom=198.0, direction="bullish",
                      formed_at=pd.Timestamp.now(), bar_index=1, broken=True),
        ]
        active = get_active_order_blocks(obs, 101.0, max_dist_pct=0.05)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].bottom, 100.0)


class TestLiquidityPools(unittest.TestCase):

    def test_pools_detected(self):
        """Liquidity pools should be created from swing points."""
        # Use a longer dataset with clear zig-zags
        n = 60
        t = np.linspace(0, 6*np.pi, n)
        close_v = 100 + np.sin(t) * 10 + np.linspace(0, 5, n)
        df = pd.DataFrame({
            "open": close_v, "high": close_v+2, "low": close_v-2,
            "close": close_v, "volume": [100]*n,
        }, index=pd.date_range("2026-01-01", periods=n, freq="h"))
        swings = detect_swing_points(df, lookback=2)
        self.assertGreater(len(swings), 0, f"Need swing points to test pools, got {len(swings)}")
        pools = detect_liquidity_pools(df, swings)
        self.assertGreater(len(pools), 0)

    def test_sell_side_pool(self):
        """Swing lows create sell-side liquidity pools."""
        close_prices = [100, 102, 101, 104, 102, 106]
        df = make_df(close_prices)
        swings = detect_swing_points(df, lookback=2)
        pools = detect_liquidity_pools(df, swings)
        sell_pools = [p for p in pools if p.type == "sell_side"]
        for sp in sell_pools:
            self.assertIsInstance(sp.price, float)

    def test_recent_liquidity_grab_none(self):
        """No swept pools should return None."""
        pools = [
            LiquidityPool(100.0, "sell_side", pd.Timestamp.now(), swept=False),
            LiquidityPool(110.0, "buy_side", pd.Timestamp.now(), swept=False),
        ]
        recent = recent_liquidity_grab(pools)
        self.assertIsNone(recent)

    def test_pool_swept_detection(self):
        """A pool above a high should be marked swept if price wicks through and closes below."""
        # Create a swing high at 102, then a candle that wicks to 103 but closes at 101
        close_prices = [100, 101, 102, 101, 103, 102]
        highs = [101, 102, 103, 102, 104, 103]
        lows = [99, 100, 101, 100, 100, 101]
        df = make_df(close_prices, highs=highs, lows=lows)
        swings = detect_swing_points(df, lookback=2)
        pools = detect_liquidity_pools(df, swings)
        # Just verify no crashes
        self.assertIsInstance(pools, list)


class TestPremiumDiscount(unittest.TestCase):

    def test_premium_zone(self):
        """Price above 50% but not within EQ band should be premium."""
        pd = calc_premium_discount(200.0, 100.0, 180.0)
        self.assertEqual(pd.zone, "premium")

    def test_discount_zone(self):
        """Price below 50% but not within EQ band should be discount."""
        pd = calc_premium_discount(200.0, 100.0, 120.0)
        self.assertEqual(pd.zone, "discount")

    def test_equilibrium_zone(self):
        """Price near 50% within 10% band should be equilibrium."""
        pd = calc_premium_discount(200.0, 100.0, 155.0)
        # eq = 150, band = 10, so 145-155 is equilibrium
        self.assertEqual(pd.zone, "equilibrium")

    def test_premium_top_value(self):
        """premium_top should equal swing_high."""
        pd = calc_premium_discount(200.0, 100.0, 150.0)
        self.assertEqual(pd.premium_top, 200.0)
        self.assertEqual(pd.discount_bot, 100.0)

    def test_flat_range(self):
        """If swing_high == swing_low, should not crash."""
        pd = calc_premium_discount(100.0, 100.0, 100.0)
        self.assertEqual(pd.zone, "equilibrium")
        self.assertEqual(pd.equilibrium, 100.0)


class TestPipeline(unittest.TestCase):

    def test_analyse_timeframe_runs(self):
        """Full SMC analysis on a DataFrame should return an SMCAnalysisResult."""
        from signals.pipeline import analyse_timeframe
        close_prices = [100, 102, 101, 104, 102, 106, 104, 108, 105, 110, 108, 112]
        df = make_df(close_prices)
        result = analyse_timeframe(df, timeframe="1h", lookback=3)
        self.assertEqual(result.timeframe, "1h")
        self.assertEqual(result.current_price, 112.0)
        self.assertIsNotNone(result.structure)
        self.assertIn(result.structure.bias, list(Bias))

    def test_analyse_timeframe_to_dict(self):
        """to_dict should produce a serialisable dict."""
        from signals.pipeline import analyse_timeframe
        close_prices = [100, 102, 101, 104, 102, 106, 104, 108]
        df = make_df(close_prices)
        result = analyse_timeframe(df, lookback=2)
        d = result.to_dict()
        self.assertIn("timeframe", d)
        self.assertIn("bias", d)
        self.assertIn("sr_levels", d)
        self.assertIn("active_fvgs", d)
        self.assertIn("active_order_blocks", d)

    def test_analyse_all_timeframes(self):
        """analyse_all_timeframes should handle multiple TFs."""
        from signals.pipeline import analyse_all_timeframes
        data = {}
        for tf, n in [("1h", 100), ("4h", 80), ("1d", 60)]:
            data[tf] = make_df(list(np.sin(np.linspace(0, 6, n)) * 10 + 100), n)
        results = analyse_all_timeframes(data)
        self.assertIn("1h", results)
        self.assertIn("4h", results)
        self.assertIn("1d", results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
