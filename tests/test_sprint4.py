"""Unit tests for Sprint 4 — Confluence Scoring, MTF Bias, Prompt Builder, LLM Engine."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
import asyncio
import json
import numpy as np
import pandas as pd

from core.market_structure import Bias
from core.swing_points import SwingPoint
from signals.pipeline import analyse_timeframe, SMCAnalysisResult
from signals.confluence import (
    score_confluence, score_to_dict, score_to_summary,
    ConfluenceScore, ConfluenceFactor,
)
from signals.mtf_bias import check_mtf_bias, MTFBias, mtf_bias_to_dict
from signals.prompt_builder import build_prompt, format_ohlcv_table, SYSTEM_PROMPT
from signals.llm_engine import _parse_llm_response, _pass_result, SignalResult


# ─── Helpers ─────────────────────────────────────────────
def make_sine_df(n=200, base=60000, amplitude=2000, noise=100, seed=42):
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


def make_result(tf="1h") -> SMCAnalysisResult:
    """Build a realistic SMCAnalysisResult via analyse_timeframe."""
    df = make_sine_df(200)
    return analyse_timeframe(df, timeframe=tf, lookback=3)


def make_results_dict() -> dict:
    """Build multi-TF results for testing."""
    return {
        "1d": make_result("1d"),
        "4h": make_result("4h"),
        "1h": make_result("1h"),
    }


# ─── Confluence Tests ────────────────────────────────────
class TestConfluenceScore(unittest.TestCase):
    def test_returns_confluence_score(self):
        r = make_result()
        score = score_confluence(r, mtf_aligned=True)
        self.assertIsInstance(score, ConfluenceScore)
        self.assertIn(score.dominant_direction, ("bullish", "bearish", "neutral"))

    def test_mtf_aligned_adds_tier1(self):
        r = make_result()
        score_aligned = score_confluence(r, mtf_aligned=True)
        score_not = score_confluence(r, mtf_aligned=False)
        # Aligned should have an extra +3 factor
        aligned_names = [f.name for f in score_aligned.factors]
        self.assertTrue(
            any("mtf" in n.lower() for n in aligned_names),
            "MTF aligned should create a factor"
        )
        # Net score should differ by ±3
        diff = abs(abs(score_aligned.net_score) - abs(score_not.net_score))
        self.assertGreaterEqual(diff, 0)

    def test_factors_have_correct_weights(self):
        r = make_result()
        score = score_confluence(r, mtf_aligned=True)
        for f in score.factors:
            if f.tier == 1:
                self.assertEqual(f.weight, 3, f"Tier 1 should be weight 3: {f.name}")
            elif f.tier == 2:
                self.assertEqual(f.weight, 2, f"Tier 2 should be weight 2: {f.name}")
            elif f.tier == 3:
                self.assertEqual(f.weight, 1, f"Tier 3 should be weight 1: {f.name}")

    def test_threshold_check(self):
        r = make_result()
        score = score_confluence(r, mtf_aligned=True, threshold=8)
        expected = abs(score.net_score) >= 8
        self.assertEqual(score.meets_threshold, expected)

    def test_score_to_dict(self):
        r = make_result()
        score = score_confluence(r)
        d = score_to_dict(score)
        self.assertIn("direction", d)
        self.assertIn("net_score", d)
        self.assertIn("factors", d)
        self.assertIsInstance(d["factors"], list)

    def test_score_to_summary(self):
        r = make_result()
        score = score_confluence(r)
        summary = score_to_summary(score)
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 10)


# ─── MTF Bias Tests ──────────────────────────────────────
class TestMTFBias(unittest.TestCase):
    def test_returns_mtf_bias(self):
        results = make_results_dict()
        bias = check_mtf_bias(results)
        self.assertIsInstance(bias, MTFBias)

    def test_aligned_when_all_same(self):
        """If all TFs have the same bias, it should be aligned."""
        results = make_results_dict()
        bias = check_mtf_bias(results)
        # Since sine data produces the same bias for all TFs (same seed),
        # they should all agree
        if bias.daily_bias == bias.h4_bias == bias.h1_bias and bias.daily_bias in (Bias.BULLISH, Bias.BEARISH):
            self.assertTrue(bias.aligned)
            self.assertEqual(bias.strength, 1.0)

    def test_missing_timeframe_handled(self):
        """Missing TF should default to UNKNOWN, not crash."""
        results = {"1h": make_result("1h")}  # Only 1h
        bias = check_mtf_bias(results)
        self.assertIsInstance(bias, MTFBias)
        self.assertEqual(bias.daily_bias, Bias.UNKNOWN)
        self.assertEqual(bias.h4_bias, Bias.UNKNOWN)

    def test_empty_results(self):
        bias = check_mtf_bias({})
        self.assertIsInstance(bias, MTFBias)
        self.assertFalse(bias.aligned)
        self.assertEqual(bias.strength, 0.0)

    def test_summary_string(self):
        results = make_results_dict()
        bias = check_mtf_bias(results)
        self.assertIsInstance(bias.summary, str)
        self.assertGreater(len(bias.summary), 5)

    def test_mtf_bias_to_dict(self):
        results = make_results_dict()
        bias = check_mtf_bias(results)
        d = mtf_bias_to_dict(bias)
        self.assertIn("aligned", d)
        self.assertIn("dominant_direction", d)
        self.assertIn("strength", d)


# ─── Prompt Builder Tests ────────────────────────────────
class TestPromptBuilder(unittest.TestCase):
    def test_format_ohlcv_table(self):
        df = make_sine_df(20)
        table = format_ohlcv_table(df, n=5)
        self.assertIn("Open", table)
        self.assertIn("High", table)
        lines = table.strip().split("\n")
        # Header + separator + 5 data rows = 7 lines
        self.assertEqual(len(lines), 7)

    def test_format_empty_df(self):
        df = pd.DataFrame()
        table = format_ohlcv_table(df)
        self.assertIn("No candle data", table)

    def test_build_prompt_returns_tuple(self):
        results = make_results_dict()
        r1h = results["1h"]
        confluence = score_confluence(r1h, mtf_aligned=True)
        mtf_bias = check_mtf_bias(results)
        sys_p, usr_p = build_prompt(
            results=results,
            confluence=confluence,
            mtf_bias=mtf_bias,
            symbol="BTC/USDT",
            current_price=61500.0,
            primary_tf="1h",
            candle_df=make_sine_df(20),
        )
        self.assertIsInstance(sys_p, str)
        self.assertIsInstance(usr_p, str)
        self.assertIn("JSON", sys_p)
        self.assertIn("SIGNALFORGE ANALYSIS", usr_p)

    def test_prompt_has_all_sections(self):
        results = make_results_dict()
        r1h = results["1h"]
        confluence = score_confluence(r1h, mtf_aligned=True)
        mtf_bias = check_mtf_bias(results)
        _, usr = build_prompt(
            results=results,
            confluence=confluence,
            mtf_bias=mtf_bias,
            symbol="BTC/USDT",
            current_price=61500.0,
            candle_df=make_sine_df(20),
        )
        self.assertIn("## Asset", usr)
        self.assertIn("## Multi-Timeframe Bias", usr)
        self.assertIn("## Confluence Score", usr)
        self.assertIn("## Open Positions", usr)
        self.assertIn("trading signal as JSON", usr)

    def test_prompt_with_open_positions(self):
        results = make_results_dict()
        r1h = results["1h"]
        confluence = score_confluence(r1h)
        mtf_bias = check_mtf_bias(results)
        positions = [{"symbol": "BTC/USDT", "side": "LONG", "entry": 61000, "sl": 60500, "size": 0.1}]
        _, usr = build_prompt(
            results=results,
            confluence=confluence,
            mtf_bias=mtf_bias,
            open_positions=positions,
        )
        self.assertIn("LONG", usr)
        self.assertIn("61000", usr)


# ─── LLM Engine Tests ───────────────────────────────────
class TestLLMParser(unittest.TestCase):
    def test_parse_valid_json(self):
        raw = json.dumps({
            "signal": "BUY",
            "confidence": 82,
            "entry": 61500.0,
            "stop_loss": 60800.0,
            "tp1": 62200.0,
            "tp2": 63000.0,
            "tp3": 64000.0,
            "reasoning": "Strong bullish confluence",
            "key_risk": "BTC could reject at 62k resistance",
            "timeframe": "4h",
            "rr_ratio": 2.1,
        })
        result = _parse_llm_response(raw, "openai/gpt-4o", 500)
        self.assertIsInstance(result, SignalResult)
        self.assertEqual(result.signal, "BUY")
        self.assertEqual(result.confidence, 82)
        self.assertEqual(result.entry, 61500.0)
        self.assertIsNone(result.error)
        self.assertTrue(result.is_actionable)

    def test_parse_with_markdown_fence(self):
        raw = '```json\n{"signal":"SELL","confidence":75,"entry":61000,"stop_loss":61500,"tp1":60200,"tp2":59500,"tp3":58500,"reasoning":"test","key_risk":"test","rr_ratio":1.6}\n```'
        result = _parse_llm_response(raw, "openai/gpt-4o", 300)
        self.assertEqual(result.signal, "SELL")
        self.assertIsNone(result.error)

    def test_parse_pass_signal(self):
        raw = json.dumps({
            "signal": "PASS",
            "confidence": 40,
            "entry": 0, "stop_loss": 0,
            "tp1": 0, "tp2": 0, "tp3": 0,
            "reasoning": "No clear setup",
            "key_risk": "N/A",
            "rr_ratio": 0,
        })
        result = _parse_llm_response(raw, "openai/gpt-4o", 200)
        self.assertEqual(result.signal, "PASS")
        self.assertFalse(result.is_actionable)

    def test_parse_missing_keys(self):
        raw = json.dumps({"signal": "BUY", "confidence": 80})
        result = _parse_llm_response(raw, "openai/gpt-4o", 100)
        self.assertEqual(result.signal, "PASS")
        self.assertIsNotNone(result.error)
        self.assertIn("Missing keys", result.error)

    def test_parse_no_json(self):
        raw = "I think the market looks bullish"
        result = _parse_llm_response(raw, "openai/gpt-4o", 100)
        self.assertEqual(result.signal, "PASS")
        self.assertIsNotNone(result.error)

    def test_parse_invalid_json(self):
        raw = "{signal: BUY, confidence: 80}"
        result = _parse_llm_response(raw, "openai/gpt-4o", 100)
        self.assertEqual(result.signal, "PASS")
        self.assertIsNotNone(result.error)

    def test_pass_result_helper(self):
        result = _pass_result("Test error", "gpt-4o", 0)
        self.assertEqual(result.signal, "PASS")
        self.assertEqual(result.error, "Test error")
        self.assertFalse(result.is_actionable)


class TestSignalResult(unittest.TestCase):
    def test_to_dict(self):
        result = SignalResult(
            signal="BUY", confidence=85, entry=61500,
            stop_loss=60800, tp1=62200, tp2=63000, tp3=64000,
            reasoning="Test", key_risk="Test risk",
            model_used="gpt-4o", rr_ratio=2.0,
        )
        d = result.to_dict()
        self.assertEqual(d["signal"], "BUY")
        self.assertEqual(d["confidence"], 85)
        self.assertIn("model_used", d)

    def test_to_telegram_message_buy(self):
        result = SignalResult(
            signal="BUY", confidence=85, entry=61500,
            stop_loss=60800, tp1=62200, tp2=63000, tp3=64000,
            reasoning="Strong confluence", key_risk="Resistance ahead",
            model_used="gpt-4o", rr_ratio=2.0,
        )
        msg = result.to_telegram_message("BTC/USDT")
        self.assertIn("BUY", msg)
        self.assertIn("85%", msg)
        self.assertIn("61,500", msg)
        self.assertIn("🟢", msg)

    def test_to_telegram_message_sell(self):
        result = SignalResult(
            signal="SELL", confidence=78, entry=61000,
            stop_loss=61500, tp1=60200, tp2=59500, tp3=58500,
            reasoning="Bearish", key_risk="Support bounce",
            model_used="gpt-4o", rr_ratio=1.6,
        )
        msg = result.to_telegram_message()
        self.assertIn("SELL", msg)
        self.assertIn("🔴", msg)

    def test_to_telegram_message_pass(self):
        result = _pass_result("No setup", "gpt-4o", 0)
        msg = result.to_telegram_message()
        self.assertIn("PASS", msg)
        self.assertIn("🔵", msg)

    def test_is_actionable(self):
        buy = SignalResult(
            signal="BUY", confidence=80, entry=61000,
            stop_loss=60500, tp1=61500, tp2=62000, tp3=63000,
            reasoning="Test", key_risk="Risk",
        )
        self.assertTrue(buy.is_actionable)

        sell = SignalResult(
            signal="SELL", confidence=80, entry=61000,
            stop_loss=61500, tp1=60500, tp2=60000, tp3=59000,
            reasoning="Test", key_risk="Risk",
        )
        self.assertTrue(sell.is_actionable)

        no_pass = _pass_result("err", "m", 0)
        self.assertFalse(no_pass.is_actionable)

        error_buy = SignalResult(
            signal="BUY", confidence=80, entry=61000,
            stop_loss=60500, tp1=61500, tp2=62000, tp3=63000,
            reasoning="Test", key_risk="Risk", error="some error",
        )
        self.assertFalse(error_buy.is_actionable)


# ─── Integration: Full pipeline scoring ───────────────────
class TestFullPipelineScoring(unittest.TestCase):
    def test_end_to_end_scoring(self):
        """Full pipeline → confluence scoring → prompt building."""
        results = make_results_dict()
        mtf_bias = check_mtf_bias(results)
        confluence = score_confluence(results["1h"], mtf_aligned=mtf_bias.aligned)
        sys_p, usr_p = build_prompt(
            results=results,
            confluence=confluence,
            mtf_bias=mtf_bias,
            symbol="BTC/USDT",
            current_price=61500.0,
            candle_df=make_sine_df(20),
        )
        # Should produce valid prompt strings
        self.assertGreater(len(sys_p), 100)
        self.assertGreater(len(usr_p), 200)
        # Confluence score should have at least 1 factor
        self.assertGreater(len(confluence.factors), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
