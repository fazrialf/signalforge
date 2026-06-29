"""Unit tests for Sprint 5 — Filter Gate, Cooldown, Risk Sizing, Signal Log."""
import sys, os, time, tempfile, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest

from signals.llm_engine import SignalResult, _pass_result
from signals.cooldown import CooldownTracker
from signals.risk_sizing import calc_position_size, validate_tp_structure, PositionSize
from signals.filter_gate import FilterGate, FilterResult
from signals.signal_log import log_signal, get_recent_signals, get_signal_stats
from signals.mtf_bias import MTFBias
from core.market_structure import Bias


# ─── Helpers ─────────────────────────────────────────────────────────────────
def make_buy_signal(confidence=82.0, rr=2.1, entry=61500, sl=60800):
    return SignalResult(
        signal="BUY", confidence=confidence,
        entry=entry, stop_loss=sl,
        tp1=62200, tp2=63000, tp3=64500,
        reasoning="Strong bullish confluence",
        key_risk="Resistance at 62k",
        rr_ratio=rr, model_used="hermes-main",
    )

def make_sell_signal(confidence=78.0, rr=1.8):
    return SignalResult(
        signal="SELL", confidence=confidence,
        entry=61000, stop_loss=61600,
        tp1=60300, tp2=59500, tp3=58000,
        reasoning="Bearish structure break",
        key_risk="Support bounce possible",
        rr_ratio=rr, model_used="hermes-main",
    )

def make_aligned_bias(direction="bullish"):
    bias = Bias.BULLISH if direction == "bullish" else Bias.BEARISH
    return MTFBias(
        daily_bias=bias, h4_bias=bias, h1_bias=bias,
        aligned=True, dominant_direction=direction,
        strength=1.0, summary=f"✅ MTF ALIGNED {direction.upper()}",
    )

def make_conflicting_bias():
    return MTFBias(
        daily_bias=Bias.BULLISH, h4_bias=Bias.BEARISH, h1_bias=Bias.BULLISH,
        aligned=False, dominant_direction="conflicting",
        strength=0.0, summary="❌ MTF CONFLICTING",
    )


# ─── CooldownTracker Tests ────────────────────────────────────────────────────
class TestCooldownTracker(unittest.TestCase):
    def test_no_cooldown_initially(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        self.assertFalse(ct.is_in_cooldown("BTC/USDT"))

    def test_set_and_check_cooldown(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        ct.set_cooldown("BTC/USDT")
        self.assertTrue(ct.is_in_cooldown("BTC/USDT"))

    def test_clear_cooldown(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        ct.set_cooldown("BTC/USDT")
        ct.clear_cooldown("BTC/USDT")
        self.assertFalse(ct.is_in_cooldown("BTC/USDT"))

    def test_time_remaining(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        ct.set_cooldown("BTC/USDT")
        remaining = ct.time_remaining("BTC/USDT")
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 30)

    def test_time_remaining_when_not_in_cooldown(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        self.assertEqual(ct.time_remaining("ETH/USDT"), 0)

    def test_custom_cooldown_duration(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        ct.set_cooldown("BTC/USDT", minutes=60)
        remaining = ct.time_remaining("BTC/USDT")
        self.assertGreater(remaining, 50)  # ~60min remaining

    def test_expired_cooldown(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        # Manually set an already-expired cooldown
        ct._cooldowns["BTC/USDT"] = time.time() - 1  # expired 1 second ago
        self.assertFalse(ct.is_in_cooldown("BTC/USDT"))
        self.assertEqual(ct.time_remaining("BTC/USDT"), 0)

    def test_persistence_with_sqlite(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            ct1 = CooldownTracker(default_cooldown_minutes=30, db_path=db_path)
            ct1.set_cooldown("BTC/USDT")
            # New instance should load the cooldown from DB
            ct2 = CooldownTracker(default_cooldown_minutes=30, db_path=db_path)
            self.assertTrue(ct2.is_in_cooldown("BTC/USDT"))
        finally:
            os.unlink(db_path)

    def test_multiple_symbols_independent(self):
        ct = CooldownTracker(default_cooldown_minutes=30)
        ct.set_cooldown("BTC/USDT")
        self.assertTrue(ct.is_in_cooldown("BTC/USDT"))
        self.assertFalse(ct.is_in_cooldown("ETH/USDT"))


# ─── Risk Sizing Tests ────────────────────────────────────────────────────────
class TestRiskSizing(unittest.TestCase):
    def test_returns_position_size(self):
        pos = calc_position_size(
            account_balance=10000, confidence=82,
            entry=61500, stop_loss=60800, side="LONG",
        )
        self.assertIsInstance(pos, PositionSize)

    def test_confidence_tier_low(self):
        """75-79% confidence -> 1.0% risk."""
        pos = calc_position_size(10000, 77, 61500, 60800, "LONG")
        self.assertAlmostEqual(pos.risk_pct, 1.0)
        self.assertAlmostEqual(pos.risk_usd, 100.0)

    def test_confidence_tier_mid(self):
        """80-89% confidence -> 1.5% risk."""
        pos = calc_position_size(10000, 85, 61500, 60800, "LONG")
        self.assertAlmostEqual(pos.risk_pct, 1.5)
        self.assertAlmostEqual(pos.risk_usd, 150.0)

    def test_confidence_tier_high(self):
        """90%+ confidence -> 2.0% risk."""
        pos = calc_position_size(10000, 92, 61500, 60800, "LONG")
        self.assertAlmostEqual(pos.risk_pct, 2.0)
        self.assertAlmostEqual(pos.risk_usd, 200.0)

    def test_size_formula(self):
        """size = risk_usd / stop_distance."""
        account = 10000
        entry, sl = 61500.0, 60800.0
        stop_distance = entry - sl  # 700
        pos = calc_position_size(account, 85, entry, sl, "LONG")
        expected_size = (account * 0.015) / stop_distance
        self.assertAlmostEqual(pos.size, expected_size, places=6)

    def test_short_side(self):
        pos = calc_position_size(10000, 80, 61000, 61600, "SHORT")
        self.assertEqual(pos.side, "SHORT")
        self.assertGreater(pos.size, 0)

    def test_notional_calculation(self):
        pos = calc_position_size(10000, 82, 61500, 60800, "LONG")
        expected_notional = pos.size * pos.entry
        self.assertAlmostEqual(pos.notional_usd, expected_notional, places=2)

    def test_validate_tp_structure_valid(self):
        signal = make_buy_signal()
        result = validate_tp_structure(signal)
        self.assertIn("valid", result)
        self.assertIsInstance(result["warnings"], list)

    def test_validate_tp_ordering(self):
        """BUY with TP2 < TP1 should warn."""
        signal = SignalResult(
            signal="BUY", confidence=80, entry=61500, stop_loss=60800,
            tp1=62000, tp2=61800, tp3=62500,  # TP2 < TP1
            reasoning="test", key_risk="test", rr_ratio=1.5,
        )
        result = validate_tp_structure(signal)
        self.assertTrue(len(result["warnings"]) > 0)

    def test_validate_tp_too_close(self):
        """TP1 within 0.5% of entry should warn."""
        signal = SignalResult(
            signal="BUY", confidence=80, entry=61500, stop_loss=60800,
            tp1=61505, tp2=62000, tp3=63000,  # TP1 0.008% from entry
            reasoning="test", key_risk="test", rr_ratio=1.5,
        )
        result = validate_tp_structure(signal)
        self.assertTrue(len(result["warnings"]) > 0)


# ─── FilterGate Tests ─────────────────────────────────────────────────────────
class TestFilterGate(unittest.TestCase):
    def _make_gate(self, cooldown_active=False):
        ct = CooldownTracker(default_cooldown_minutes=30)
        if cooldown_active:
            ct.set_cooldown("BTC/USDT")
        return FilterGate(cooldown_tracker=ct, config={
            'min_confidence': 75, 'min_rr': 1.5,
            'max_active_signals': 3, 'max_heat': 6.0,
        })

    def test_all_filters_pass(self):
        gate = self._make_gate()
        result = gate.apply(
            signal=make_buy_signal(confidence=82, rr=2.1),
            mtf_bias=make_aligned_bias("bullish"),
            symbol="BTC/USDT",
            active_positions=[],
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "")

    def test_filter_low_confidence(self):
        gate = self._make_gate()
        result = gate.apply(
            signal=make_buy_signal(confidence=60, rr=2.0),
            mtf_bias=make_aligned_bias("bullish"),
            symbol="BTC/USDT",
        )
        self.assertFalse(result.passed)
        self.assertIn("confidence", result.reason.lower())

    def test_filter_low_rr(self):
        gate = self._make_gate()
        result = gate.apply(
            signal=make_buy_signal(confidence=82, rr=0.7),
            mtf_bias=make_aligned_bias("bullish"),
            symbol="BTC/USDT",
        )
        self.assertFalse(result.passed)
        self.assertIn("r:r", result.reason.lower())

    def test_filter_mtf_not_aligned(self):
        gate = self._make_gate()
        result = gate.apply(
            signal=make_buy_signal(confidence=82, rr=2.0),
            mtf_bias=make_conflicting_bias(),
            symbol="BTC/USDT",
        )
        self.assertFalse(result.passed)
        self.assertIn("mtf", result.reason.lower())

    def test_filter_cooldown(self):
        gate = self._make_gate(cooldown_active=True)
        result = gate.apply(
            signal=make_buy_signal(confidence=82, rr=2.0),
            mtf_bias=make_aligned_bias("bullish"),
            symbol="BTC/USDT",
        )
        self.assertFalse(result.passed)
        self.assertIn("cooldown", result.reason.lower())

    def test_filter_max_positions(self):
        gate = self._make_gate()
        active = [
            {"symbol": "BTC/USDT", "side": "LONG", "risk_pct": 1.5},
            {"symbol": "ETH/USDT", "side": "LONG", "risk_pct": 1.5},
            {"symbol": "SOL/USDT", "side": "LONG", "risk_pct": 1.5},
        ]
        result = gate.apply(
            signal=make_buy_signal(confidence=82, rr=2.0),
            mtf_bias=make_aligned_bias("bullish"),
            symbol="BTC/USDT",
            active_positions=active,
        )
        self.assertFalse(result.passed)
        self.assertTrue(
            "position" in result.reason.lower() or "signal" in result.reason.lower()
        )

    def test_filter_portfolio_heat(self):
        gate = self._make_gate()
        active = [
            {"symbol": "BTC/USDT", "side": "LONG", "risk_pct": 3.0},
            {"symbol": "ETH/USDT", "side": "LONG", "risk_pct": 3.5},
        ]  # total heat = 6.5% > 6.0% limit
        result = gate.apply(
            signal=make_buy_signal(confidence=82, rr=2.0),
            mtf_bias=make_aligned_bias("bullish"),
            symbol="BTC/USDT",
            active_positions=active,
        )
        self.assertFalse(result.passed)
        self.assertIn("heat", result.reason.lower())

    def test_filter_result_fields(self):
        gate = self._make_gate()
        result = gate.apply(
            signal=make_buy_signal(confidence=50),  # will fail confidence
            mtf_bias=make_aligned_bias(),
            symbol="BTC/USDT",
        )
        self.assertIsInstance(result.passed, bool)
        self.assertIsInstance(result.reason, str)
        self.assertIsInstance(result.filter_name, str)
        self.assertFalse(result.passed)
        self.assertNotEqual(result.filter_name, "")

    def test_sell_signal_with_aligned_bearish(self):
        gate = self._make_gate()
        result = gate.apply(
            signal=make_sell_signal(confidence=78, rr=1.8),
            mtf_bias=make_aligned_bias("bearish"),
            symbol="BTC/USDT",
            active_positions=[],
        )
        self.assertTrue(result.passed)


# ─── Signal Log Tests ─────────────────────────────────────────────────────────
class TestSignalLog(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.db_path = self.db.name
        self.db.close()
        # Create the base signals table (from db/schema.py)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                symbol TEXT,
                direction TEXT,
                entry_price REAL,
                sl REAL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                confidence REAL,
                reasoning TEXT,
                model TEXT,
                status TEXT,
                timeframe TEXT,
                rr_ratio REAL,
                key_risk TEXT
            )
        """)
        conn.commit()
        conn.close()
        # Let signal_log add its Sprint 5 columns via _ensure_columns
        from signals.signal_log import _ensure_columns
        conn = sqlite3.connect(self.db_path)
        _ensure_columns(conn)
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_log_signal_returns_id(self):
        sig = make_buy_signal()
        row_id = log_signal(
            signal=sig, symbol="BTC/USDT",
            confluence_score=15, mtf_aligned=True,
            filter_result="delivered",
            db_path=self.db_path,
        )
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)

    def test_get_recent_signals(self):
        sig = make_buy_signal()
        log_signal(sig, "BTC/USDT", 15, True, "delivered", db_path=self.db_path)
        log_signal(sig, "BTC/USDT", 12, True, "filtered: low R:R", db_path=self.db_path)
        recent = get_recent_signals(limit=10, db_path=self.db_path)
        self.assertEqual(len(recent), 2)

    def test_get_recent_signals_by_symbol(self):
        sig = make_buy_signal()
        log_signal(sig, "BTC/USDT", 15, True, "delivered", db_path=self.db_path)
        log_signal(sig, "ETH/USDT", 10, False, "delivered", db_path=self.db_path)
        btc_signals = get_recent_signals(symbol="BTC/USDT", limit=10, db_path=self.db_path)
        self.assertEqual(len(btc_signals), 1)

    def test_get_signal_stats(self):
        sig = make_buy_signal()
        log_signal(sig, "BTC/USDT", 15, True, "delivered", db_path=self.db_path)
        log_signal(sig, "BTC/USDT", 8, True, "filtered: low R:R", db_path=self.db_path)
        pass_sig = _pass_result("no setup", "hermes-main", 0)
        log_signal(pass_sig, "BTC/USDT", 5, False, "pass", db_path=self.db_path)
        stats = get_signal_stats(db_path=self.db_path)
        self.assertIn("total_signals", stats)
        self.assertEqual(stats["total_signals"], 3)

    def test_pass_signal_logging(self):
        sig = _pass_result("no confluence", "hermes-main", 200)
        row_id = log_signal(
            signal=sig, symbol="BTC/USDT",
            confluence_score=3, mtf_aligned=False,
            filter_result="pass",
            db_path=self.db_path,
        )
        self.assertIsInstance(row_id, int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
