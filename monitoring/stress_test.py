"""
Sprint 8 — Stress Test: Replay 7-day history, measure throughput & filter rates.

Usage:
    python3 -m monitoring.stress_test
    python3 -m monitoring.stress_test --days 3 --symbol BTC/USDT
"""
import argparse
import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
import sys, os

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import DB_PATH, MIN_CONFLUENCE_SCORE, MIN_LLM_CONFIDENCE, MIN_RR_RATIO
from config.assets import get_enabled_assets, ASSETS
from signals.pipeline import analyse_all_timeframes
from signals.confluence import score_confluence, ConfluenceScore
from signals.mtf_bias import check_mtf_bias
from signals.filter_gate import FilterGate
from signals.cooldown import CooldownTracker

logger = logging.getLogger("stress_test")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@dataclass
class StressResult:
    symbol: str
    days: int
    total_candles: int = 0
    total_cycles: int = 0
    confluence_triggered: int = 0   # cycles where score >= threshold
    confluence_skipped: int = 0     # cycles where score < threshold
    filter_passed: int = 0
    filter_blocked: int = 0
    filter_breakdown: dict = field(default_factory=dict)
    errors: int = 0
    duration_seconds: float = 0.0
    avg_cycle_ms: float = 0.0
    throughput_cps: float = 0.0     # cycles per second


def load_candles(db_path: str, symbol: str, timeframe: str, days: int) -> list[dict]:
    """Load recent OHLCV candles for a given symbol/timeframe."""
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND timeframe = ? AND ts >= ? ORDER BY ts ASC",
        (symbol, timeframe, cutoff_ms),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_dataframes(rows: list[dict]) -> object:
    """Convert candle rows to a pandas-like DataFrame for analysis."""
    import pandas as pd
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.set_index("timestamp")
    df.drop(columns=["ts"], inplace=True)
    return df


def run_stress_test(
    symbol: str = "BTC/USDT",
    days: int = 7,
    db_path: str = None,
    window_size: int = 200,     # rolling window of candles per cycle
    step_size: int = 1,         # step by N candles per cycle
    verbose: bool = False,
) -> StressResult:
    """
    Replay historical candles in a rolling window, running the full pipeline
    (SMC analysis → confluence scoring → MTF bias → filter gate) on each window.

    This measures:
    - Pipeline throughput (cycles/sec)
    - Confluence trigger rate
    - Filter pass/block rates and breakdown
    - Error rate
    """
    import tempfile
    db_path = db_path or str(DB_PATH)
    result = StressResult(symbol=symbol, days=days)

    # Load candles for the primary timeframes
    timeframes = ["1h", "4h", "1d"]
    all_candles: dict[str, list] = {}
    for tf in timeframes:
        rows = load_candles(db_path, symbol, tf, days)
        all_candles[tf] = rows
        if verbose:
            logger.info("[%s] Loaded %d candles for %s", symbol, len(rows), tf)

    primary_rows = all_candles.get("1h", [])
    result.total_candles = len(primary_rows)

    if len(primary_rows) < window_size:
        logger.warning(
            "[%s] Only %d candles available (need %d minimum). "
            "Reduce --days or --window.",
            symbol, len(primary_rows), window_size,
        )
        # Use whatever is available
        window_size = max(50, len(primary_rows) // 2)

    # Use a temp DB for cooldown (don't pollute live DB)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name

    cooldown = CooldownTracker(default_cooldown_minutes=30, db_path=tmp_db)
    filter_gate = FilterGate(
        cooldown_tracker=cooldown,
        config={
            "min_confidence": MIN_LLM_CONFIDENCE,
            "min_rr": MIN_RR_RATIO,
            "max_active_signals": 3,
            "max_heat": 6.0,
        },
    )

    start_ts = time.time()
    cycle_times = []

    # Build 4h and 1d DFs once (they don't change per cycle)
    import pandas as pd
    static_dfs: dict[str, pd.DataFrame] = {}
    for tf in ["4h", "1d"]:
        static_dfs[tf] = build_dataframes(all_candles.get(tf, []))

    # Rolling window over 1h candles
    indices = range(window_size, len(primary_rows), step_size)
    if not indices:
        indices = [len(primary_rows)]

    for i in indices:
        cycle_start = time.perf_counter()
        result.total_cycles += 1

        try:
            # Build fresh_data for this window
            window_rows = primary_rows[max(0, i - window_size): i]
            fresh_data = {
                "1h": build_dataframes(window_rows),
                "4h": static_dfs["4h"],
                "1d": static_dfs["1d"],
            }

            # Skip empty windows
            if fresh_data["1h"].empty:
                continue

            current_price = float(window_rows[-1]["close"]) if window_rows else 0.0

            # Run full SMC pipeline
            smc_results = analyse_all_timeframes(fresh_data, current_price=current_price)

            # Confluence scoring (use primary timeframe result)
            primary_result = smc_results.get("1h") or smc_results.get("4h") or list(smc_results.values())[0]
            confluence = score_confluence(primary_result, mtf_aligned=False)

            if abs(confluence.net_score) < MIN_CONFLUENCE_SCORE:
                result.confluence_skipped += 1
            else:
                result.confluence_triggered += 1

                # MTF bias
                mtf_bias = check_mtf_bias(smc_results)

                # Simulate a LLM signal result with a synthetic confidence
                # (we don't call the real LLM in stress tests)
                from signals.llm_engine import SignalResult
                synthetic_signal = SignalResult(
                    signal="BUY" if confluence.net_score > 0 else "SELL",
                    confidence=0.78,
                    entry=current_price,
                    stop_loss=current_price * 0.98 if confluence.net_score > 0 else current_price * 1.02,
                    tp1=current_price * 1.015,
                    tp2=current_price * 1.025,
                    tp3=current_price * 1.04,
                    rr_ratio=2.0,
                    reasoning="stress-test synthetic signal",
                    key_risk="stress test — no real risk",
                    error=None,
                )

                # Run filter gate
                filter_result = filter_gate.apply(
                    signal=synthetic_signal,
                    mtf_bias=mtf_bias,
                    symbol=symbol,
                    active_positions=[],
                )

                if filter_result.passed:
                    result.filter_passed += 1
                else:
                    result.filter_blocked += 1
                    reason = filter_result.reason or "unknown"
                    result.filter_breakdown[reason] = result.filter_breakdown.get(reason, 0) + 1

        except Exception as e:
            result.errors += 1
            if verbose:
                logger.exception("[%s] Cycle %d error: %s", symbol, result.total_cycles, e)

        cycle_ms = (time.perf_counter() - cycle_start) * 1000
        cycle_times.append(cycle_ms)

    # Cleanup temp DB
    try:
        os.unlink(tmp_db)
    except Exception:
        pass

    result.duration_seconds = time.time() - start_ts
    result.avg_cycle_ms = sum(cycle_times) / len(cycle_times) if cycle_times else 0.0
    result.throughput_cps = result.total_cycles / result.duration_seconds if result.duration_seconds > 0 else 0.0

    return result


def format_report(r: StressResult) -> str:
    """Format a StressResult as a human-readable report."""
    total = r.confluence_triggered + r.confluence_skipped
    conf_rate = (r.confluence_triggered / total * 100) if total > 0 else 0.0
    gate_total = r.filter_passed + r.filter_blocked
    pass_rate = (r.filter_passed / gate_total * 100) if gate_total > 0 else 0.0

    lines = [
        f"═══ STRESS TEST REPORT: {r.symbol} ═══",
        f"Days replayed:     {r.days}d",
        f"Total candles:     {r.total_candles:,}",
        f"Cycles run:        {r.total_cycles:,}",
        f"Duration:          {r.duration_seconds:.1f}s",
        f"Avg cycle time:    {r.avg_cycle_ms:.1f}ms",
        f"Throughput:        {r.throughput_cps:.1f} cycles/sec",
        f"",
        f"── Confluence Gate ──",
        f"Triggered:         {r.confluence_triggered} ({conf_rate:.1f}%)",
        f"Skipped:           {r.confluence_skipped} ({100-conf_rate:.1f}%)",
        f"",
        f"── Filter Gate ──",
        f"Passed:            {r.filter_passed} ({pass_rate:.1f}%)",
        f"Blocked:           {r.filter_blocked} ({100-pass_rate:.1f}%)",
    ]

    if r.filter_breakdown:
        lines.append("Block reasons:")
        for reason, count in sorted(r.filter_breakdown.items(), key=lambda x: -x[1]):
            lines.append(f"  {reason}: {count}")

    lines += [
        f"",
        f"── Health ──",
        f"Errors:            {r.errors}",
        f"Error rate:        {r.errors/r.total_cycles*100:.2f}%" if r.total_cycles > 0 else "Error rate: N/A",
    ]
    return "\n".join(lines)


def run_all_assets(days: int = 7, verbose: bool = False) -> list[StressResult]:
    """Run stress test for all enabled assets."""
    results = []
    for asset in get_enabled_assets():
        logger.info("Running stress test for %s...", asset.symbol)
        r = run_stress_test(symbol=asset.symbol, days=days, verbose=verbose)
        results.append(r)
        print(format_report(r))
        print()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SignalForge stress test")
    parser.add_argument("--days", type=int, default=7, help="Days of history to replay")
    parser.add_argument("--symbol", type=str, default=None, help="Single symbol to test (default: all enabled)")
    parser.add_argument("--window", type=int, default=200, help="Rolling window size in candles")
    parser.add_argument("--step", type=int, default=5, help="Step size between cycles")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.symbol:
        r = run_stress_test(
            symbol=args.symbol, days=args.days,
            window_size=args.window, step_size=args.step,
            verbose=args.verbose,
        )
        print(format_report(r))
    else:
        run_all_assets(days=args.days, verbose=args.verbose)
