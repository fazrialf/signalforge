"""
SignalForge – Candlestick Pattern Detection Module
===================================================
Detects classic candlestick patterns on OHLCV DataFrames.
Pure pandas/numpy implementation (no pandas-ta dependency).

Supported patterns:
  • Bullish / Bearish Engulfing
  • Pin Bar (Hammer / Shooting Star)
  • Doji (Standard / Dragonfly / Gravestone)
  • Morning Star / Evening Star
  • Three White Soldiers / Three Black Crows
  • Tweezer Top / Tweezer Bottom
  • Inside Bar
  • Marubozu (Bullish / Bearish)

Usage::

    from signalforge.core.candlestick import detect_all_patterns, patterns_to_dict
    patterns = detect_all_patterns(df)
    print(patterns_to_dict(patterns))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CandlestickPattern:
    """Represents a single detected candlestick pattern occurrence."""

    bar_index: int          # positional index in the DataFrame (iloc-based)
    timestamp: pd.Timestamp # datetime of the bar
    pattern: str            # machine-readable name, e.g. 'bullish_engulfing'
    direction: str          # 'bullish' or 'bearish'
    strength: float         # confidence / quality score 0.0 – 1.0
    description: str        # human-readable explanation


# ---------------------------------------------------------------------------
# Internal helper functions
# ---------------------------------------------------------------------------

def _body_size(row: pd.Series) -> float:
    """Absolute size of the candle body (|close − open|)."""
    return abs(float(row["close"]) - float(row["open"]))


def _range_size(row: pd.Series) -> float:
    """Total range of the candle (high − low)."""
    return float(row["high"]) - float(row["low"])


def _body_pct(row: pd.Series) -> float:
    """Body as a fraction of the total range.  Returns 0.0 if range is zero."""
    rng = _range_size(row)
    return _body_size(row) / rng if rng > 0 else 0.0


def _is_bullish(row: pd.Series) -> bool:
    """True when close ≥ open (green candle)."""
    return float(row["close"]) >= float(row["open"])


def _upper_wick(row: pd.Series) -> float:
    """Upper shadow length: high − max(open, close)."""
    return float(row["high"]) - max(float(row["open"]), float(row["close"]))


def _lower_wick(row: pd.Series) -> float:
    """Lower shadow length: min(open, close) − low."""
    return min(float(row["open"]), float(row["close"])) - float(row["low"])


# Vectorised helpers (operate on whole columns for speed where useful)

def _v_body_size(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def _v_range_size(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df["low"]


def _v_body_pct(df: pd.DataFrame) -> pd.Series:
    rng = _v_range_size(df)
    return _v_body_size(df) / rng.replace(0, np.nan)


def _v_is_bullish(df: pd.DataFrame) -> pd.Series:
    return df["close"] >= df["open"]


def _v_upper_wick(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df[["open", "close"]].max(axis=1)


def _v_lower_wick(df: pd.DataFrame) -> pd.Series:
    return df[["open", "close"]].min(axis=1) - df["low"]


def _ts(df: pd.DataFrame, iloc_idx: int) -> pd.Timestamp:
    """Safely extract the timestamp for a positional index."""
    return pd.Timestamp(df.index[iloc_idx])


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------

def detect_engulfing(
    df: pd.DataFrame,
    min_body_pct: float = 0.6,
) -> list[CandlestickPattern]:
    """Detect bullish and bearish engulfing patterns.

    *Bullish engulfing*: previous candle is bearish, current is bullish, and the
    current body completely engulfs the previous body.  Both bodies must fill at
    least *min_body_pct* of their respective ranges.

    *Bearish engulfing*: mirror image.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    min_body_pct : float
        Minimum body-to-range ratio for both candles (default 0.6).

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 2:
        return results

    bp = _v_body_pct(df)

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        # Both candles must have meaningful bodies
        if bp.iloc[i - 1] < min_body_pct or bp.iloc[i] < min_body_pct:
            continue

        prev_open, prev_close = float(prev["open"]), float(prev["close"])
        curr_open, curr_close = float(curr["open"]), float(curr["close"])

        prev_bullish = prev_close >= prev_open
        curr_bullish = curr_close >= curr_open

        # Bullish engulfing: prev bearish, curr bullish, curr body wraps prev body
        if not prev_bullish and curr_bullish:
            if curr_open <= prev_close and curr_close >= prev_open:
                strength = min(1.0, bp.iloc[i] * 1.2)
                results.append(CandlestickPattern(
                    bar_index=i,
                    timestamp=_ts(df, i),
                    pattern="bullish_engulfing",
                    direction="bullish",
                    strength=round(strength, 4),
                    description=(
                        "Bullish engulfing – current bullish body fully "
                        "engulfs previous bearish body"
                    ),
                ))

        # Bearish engulfing: prev bullish, curr bearish, curr body wraps prev body
        elif prev_bullish and not curr_bullish:
            if curr_open >= prev_close and curr_close <= prev_open:
                strength = min(1.0, bp.iloc[i] * 1.2)
                results.append(CandlestickPattern(
                    bar_index=i,
                    timestamp=_ts(df, i),
                    pattern="bearish_engulfing",
                    direction="bearish",
                    strength=round(strength, 4),
                    description=(
                        "Bearish engulfing – current bearish body fully "
                        "engulfs previous bullish body"
                    ),
                ))

    return results


def detect_pin_bar(
    df: pd.DataFrame,
    min_wick_ratio: float = 2.0,
    max_body_pct: float = 0.35,
) -> list[CandlestickPattern]:
    """Detect pin bars (hammer / shooting star).

    A pin bar has a small body and one dominant wick at least *min_wick_ratio*
    times the body size.

    * **Hammer** (bullish): long lower wick, body near the top.
    * **Shooting star** (bearish): long upper wick, body near the bottom.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    min_wick_ratio : float
        Minimum ratio of dominant wick to body size (default 2.0).
    max_body_pct : float
        Maximum body-to-range ratio (default 0.35).

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 1:
        return results

    for i in range(len(df)):
        row = df.iloc[i]
        rng = _range_size(row)
        if rng == 0:
            continue
        body = _body_size(row)
        if body == 0:
            body = rng * 0.001  # avoid division by zero for ratio check

        bpct = body / rng
        if bpct > max_body_pct:
            continue

        uw = _upper_wick(row)
        lw = _lower_wick(row)

        # Hammer: lower wick dominates
        if lw >= min_wick_ratio * body and lw > uw:
            strength = min(1.0, (lw / body) / (min_wick_ratio * 2))
            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="hammer",
                direction="bullish",
                strength=round(strength, 4),
                description=(
                    f"Hammer (pin bar) – lower wick {lw / body:.1f}× body, "
                    f"body {bpct:.0%} of range"
                ),
            ))

        # Shooting star: upper wick dominates
        elif uw >= min_wick_ratio * body and uw > lw:
            strength = min(1.0, (uw / body) / (min_wick_ratio * 2))
            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="shooting_star",
                direction="bearish",
                strength=round(strength, 4),
                description=(
                    f"Shooting star (pin bar) – upper wick {uw / body:.1f}× body, "
                    f"body {bpct:.0%} of range"
                ),
            ))

    return results


def detect_doji(
    df: pd.DataFrame,
    max_body_pct: float = 0.1,
) -> list[CandlestickPattern]:
    """Detect doji candles (body < *max_body_pct* of range).

    Sub-types:
    * **Dragonfly doji**: lower wick >> upper wick (bullish).
    * **Gravestone doji**: upper wick >> lower wick (bearish).
    * **Standard doji**: wicks roughly equal (neutral → reported as bullish).

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    max_body_pct : float
        Maximum body-to-range ratio to qualify as doji (default 0.1).

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 1:
        return results

    for i in range(len(df)):
        row = df.iloc[i]
        rng = _range_size(row)
        if rng == 0:
            continue

        bpct = _body_size(row) / rng
        if bpct > max_body_pct:
            continue

        uw = _upper_wick(row)
        lw = _lower_wick(row)

        # Classify sub-type by wick dominance (threshold: 3× the other)
        if lw > 3 * uw and lw > 0:
            pattern_name = "dragonfly_doji"
            direction = "bullish"
            desc = "Dragonfly doji – long lower shadow, bullish reversal signal"
            strength = min(1.0, lw / rng + 0.3)
        elif uw > 3 * lw and uw > 0:
            pattern_name = "gravestone_doji"
            direction = "bearish"
            desc = "Gravestone doji – long upper shadow, bearish reversal signal"
            strength = min(1.0, uw / rng + 0.3)
        else:
            pattern_name = "doji"
            direction = "bullish"  # neutral; default bullish for scoring
            desc = "Standard doji – indecision, tiny body relative to range"
            strength = 0.4

        results.append(CandlestickPattern(
            bar_index=i,
            timestamp=_ts(df, i),
            pattern=pattern_name,
            direction=direction,
            strength=round(strength, 4),
            description=desc,
        ))

    return results


def detect_morning_evening_star(
    df: pd.DataFrame,
) -> list[CandlestickPattern]:
    """Detect three-candle morning star and evening star patterns.

    **Morning star** (bullish):
      1. Large bearish candle
      2. Small-bodied candle (ideally gaps down)
      3. Large bullish candle closing above the midpoint of candle 1

    **Evening star** (bearish): mirror image.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 3:
        return results

    bp = _v_body_pct(df)
    bullish = _v_is_bullish(df)

    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c2 = df.iloc[i - 1]
        c3 = df.iloc[i]

        bp1 = bp.iloc[i - 2]
        bp2 = bp.iloc[i - 1]
        bp3 = bp.iloc[i]

        # Skip if body percentages are NaN (zero-range bars)
        if np.isnan(bp1) or np.isnan(bp2) or np.isnan(bp3):
            continue

        c1_mid = (float(c1["open"]) + float(c1["close"])) / 2

        # --- Morning star ---
        if (
            not bullish.iloc[i - 2]        # C1 bearish
            and bp1 >= 0.5                  # C1 large body
            and bp2 <= 0.35                 # C2 small body (star)
            and bullish.iloc[i]             # C3 bullish
            and bp3 >= 0.5                  # C3 large body
            and float(c3["close"]) > c1_mid # C3 closes above C1 midpoint
        ):
            # Bonus for gap between C1 close and C2 body
            gap = float(c1["close"]) - max(float(c2["open"]), float(c2["close"]))
            gap_bonus = 0.1 if gap > 0 else 0.0
            strength = min(1.0, 0.6 + gap_bonus + bp3 * 0.3)
            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="morning_star",
                direction="bullish",
                strength=round(strength, 4),
                description="Morning star – 3-candle bullish reversal pattern",
            ))

        # --- Evening star ---
        if (
            bullish.iloc[i - 2]             # C1 bullish
            and bp1 >= 0.5                   # C1 large body
            and bp2 <= 0.35                  # C2 small body (star)
            and not bullish.iloc[i]          # C3 bearish
            and bp3 >= 0.5                   # C3 large body
            and float(c3["close"]) < c1_mid  # C3 closes below C1 midpoint
        ):
            gap = max(float(c2["open"]), float(c2["close"])) - float(c1["close"])
            gap_bonus = 0.1 if gap > 0 else 0.0
            strength = min(1.0, 0.6 + gap_bonus + bp3 * 0.3)
            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="evening_star",
                direction="bearish",
                strength=round(strength, 4),
                description="Evening star – 3-candle bearish reversal pattern",
            ))

    return results


def detect_three_soldiers_crows(
    df: pd.DataFrame,
    min_body_pct: float = 0.6,
) -> list[CandlestickPattern]:
    """Detect Three White Soldiers and Three Black Crows.

    **Three White Soldiers** (bullish): three consecutive bullish candles, each
    with a strong body (*min_body_pct*) and each close higher than the last.

    **Three Black Crows** (bearish): three consecutive bearish candles with
    strong bodies and each close lower than the last.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    min_body_pct : float
        Minimum body-to-range ratio for each candle (default 0.6).

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 3:
        return results

    bp = _v_body_pct(df)
    bullish = _v_is_bullish(df)

    for i in range(2, len(df)):
        bps = [bp.iloc[i - 2], bp.iloc[i - 1], bp.iloc[i]]

        # All three must have meaningful bodies
        if any(np.isnan(b) or b < min_body_pct for b in bps):
            continue

        closes = [float(df.iloc[i - 2]["close"]),
                  float(df.iloc[i - 1]["close"]),
                  float(df.iloc[i]["close"])]

        # --- Three White Soldiers ---
        if (
            bullish.iloc[i - 2]
            and bullish.iloc[i - 1]
            and bullish.iloc[i]
            and closes[1] > closes[0]
            and closes[2] > closes[1]
        ):
            avg_bp = sum(bps) / 3
            strength = min(1.0, avg_bp * 1.1)
            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="three_white_soldiers",
                direction="bullish",
                strength=round(strength, 4),
                description=(
                    "Three White Soldiers – three consecutive strong bullish "
                    "candles with rising closes"
                ),
            ))

        # --- Three Black Crows ---
        elif (
            not bullish.iloc[i - 2]
            and not bullish.iloc[i - 1]
            and not bullish.iloc[i]
            and closes[1] < closes[0]
            and closes[2] < closes[1]
        ):
            avg_bp = sum(bps) / 3
            strength = min(1.0, avg_bp * 1.1)
            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="three_black_crows",
                direction="bearish",
                strength=round(strength, 4),
                description=(
                    "Three Black Crows – three consecutive strong bearish "
                    "candles with declining closes"
                ),
            ))

    return results


def detect_tweezer(
    df: pd.DataFrame,
    tolerance_pct: float = 0.001,
) -> list[CandlestickPattern]:
    """Detect Tweezer Top and Tweezer Bottom patterns.

    Two consecutive candles sharing (approximately) the same high (**tweezer
    top**) or the same low (**tweezer bottom**).  "Approximately" is controlled
    by *tolerance_pct* relative to the price level.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    tolerance_pct : float
        Max relative difference between highs/lows (default 0.001 = 0.1 %).

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 2:
        return results

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        prev_high = float(prev["high"])
        curr_high = float(curr["high"])
        prev_low = float(prev["low"])
        curr_low = float(curr["low"])

        # Tweezer top: matching highs, second candle ideally bearish
        if prev_high > 0:
            high_diff = abs(curr_high - prev_high) / prev_high
            if high_diff <= tolerance_pct:
                # Stronger if first bullish, second bearish (reversal)
                reversal = _is_bullish(prev) and not _is_bullish(curr)
                strength = 0.75 if reversal else 0.55
                results.append(CandlestickPattern(
                    bar_index=i,
                    timestamp=_ts(df, i),
                    pattern="tweezer_top",
                    direction="bearish",
                    strength=round(strength, 4),
                    description=(
                        f"Tweezer top – two candles with matching highs "
                        f"(diff {high_diff:.4%})"
                    ),
                ))

        # Tweezer bottom: matching lows, second candle ideally bullish
        if prev_low > 0:
            low_diff = abs(curr_low - prev_low) / prev_low
            if low_diff <= tolerance_pct:
                reversal = not _is_bullish(prev) and _is_bullish(curr)
                strength = 0.75 if reversal else 0.55
                results.append(CandlestickPattern(
                    bar_index=i,
                    timestamp=_ts(df, i),
                    pattern="tweezer_bottom",
                    direction="bullish",
                    strength=round(strength, 4),
                    description=(
                        f"Tweezer bottom – two candles with matching lows "
                        f"(diff {low_diff:.4%})"
                    ),
                ))

    return results


def detect_inside_bar(
    df: pd.DataFrame,
) -> list[CandlestickPattern]:
    """Detect inside bar patterns.

    An **inside bar** occurs when the current bar's high is lower than the
    previous bar's high *and* the current bar's low is higher than the previous
    bar's low (the current bar is fully contained within the prior bar).

    Direction is inferred from the current bar's close relative to open.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 2:
        return results

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        if (
            float(curr["high"]) < float(prev["high"])
            and float(curr["low"]) > float(prev["low"])
        ):
            direction = "bullish" if _is_bullish(curr) else "bearish"
            # Strength based on how "compressed" the inside bar is relative
            # to the mother bar — tighter compression = stronger signal
            mother_range = _range_size(prev)
            if mother_range > 0:
                compression = 1.0 - (_range_size(curr) / mother_range)
                strength = min(1.0, 0.4 + compression * 0.6)
            else:
                strength = 0.5

            results.append(CandlestickPattern(
                bar_index=i,
                timestamp=_ts(df, i),
                pattern="inside_bar",
                direction=direction,
                strength=round(strength, 4),
                description=(
                    "Inside bar – current bar completely contained within "
                    "the previous bar's range (consolidation/breakout setup)"
                ),
            ))

    return results


def detect_marubozu(
    df: pd.DataFrame,
    min_body_pct: float = 0.9,
) -> list[CandlestickPattern]:
    """Detect marubozu candles (body fills 90 %+ of the range).

    A **marubozu** has virtually no wicks, indicating strong momentum.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    min_body_pct : float
        Minimum body-to-range ratio to qualify (default 0.9).

    Returns
    -------
    list[CandlestickPattern]
    """
    results: list[CandlestickPattern] = []
    if len(df) < 1:
        return results

    bp = _v_body_pct(df)

    for i in range(len(df)):
        bpct = bp.iloc[i]
        if np.isnan(bpct) or bpct < min_body_pct:
            continue

        direction = "bullish" if _is_bullish(df.iloc[i]) else "bearish"
        strength = min(1.0, bpct)
        results.append(CandlestickPattern(
            bar_index=i,
            timestamp=_ts(df, i),
            pattern=f"{direction}_marubozu",
            direction=direction,
            strength=round(strength, 4),
            description=(
                f"{'Bullish' if direction == 'bullish' else 'Bearish'} marubozu "
                f"– body fills {bpct:.0%} of the range, strong momentum"
            ),
        ))

    return results


# ---------------------------------------------------------------------------
# Aggregation & utility functions
# ---------------------------------------------------------------------------

def detect_all_patterns(df: pd.DataFrame) -> list[CandlestickPattern]:
    """Run all candlestick detectors and return a deduplicated, sorted list.

    Deduplication rule: when the same bar index produces multiple patterns
    with the **same direction**, only the pattern with the highest strength
    is kept.  Different directions on the same bar are both retained (e.g. a
    tweezer top *and* tweezer bottom could theoretically co-exist).

    Results are sorted by ``bar_index`` **descending** (most recent first).

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex and columns:
        ``open``, ``high``, ``low``, ``close``, ``volume``.

    Returns
    -------
    list[CandlestickPattern]
    """
    if df is None or df.empty:
        return []

    # Collect from every detector
    all_patterns: list[CandlestickPattern] = []
    detectors = [
        detect_engulfing,
        detect_pin_bar,
        detect_doji,
        detect_morning_evening_star,
        detect_three_soldiers_crows,
        detect_tweezer,
        detect_inside_bar,
        detect_marubozu,
    ]

    for detector in detectors:
        try:
            all_patterns.extend(detector(df))
        except Exception:
            logger.exception("Error in detector %s", detector.__name__)

    # Deduplicate: same (bar_index, direction) → keep highest strength
    best: dict[tuple[int, str], CandlestickPattern] = {}
    for p in all_patterns:
        key = (p.bar_index, p.direction)
        if key not in best or p.strength > best[key].strength:
            best[key] = p

    # Sort by bar_index descending (most recent first)
    result = sorted(best.values(), key=lambda p: p.bar_index, reverse=True)

    logger.debug(
        "detect_all_patterns: %d raw → %d deduplicated patterns",
        len(all_patterns),
        len(result),
    )
    return result


def patterns_to_dict(patterns: list[CandlestickPattern]) -> list[dict]:
    """Convert a list of :class:`CandlestickPattern` to JSON-serialisable dicts.

    Timestamps are converted to ISO-8601 strings for JSON compatibility.

    Parameters
    ----------
    patterns : list[CandlestickPattern]

    Returns
    -------
    list[dict]
    """
    out: list[dict] = []
    for p in patterns:
        d = asdict(p)
        # Ensure timestamp is serialisable
        d["timestamp"] = str(d["timestamp"])
        out.append(d)
    return out


def recent_patterns(
    patterns: list[CandlestickPattern],
    n: int = 5,
) -> list[CandlestickPattern]:
    """Return the *n* most recent patterns (by bar_index, descending).

    Parameters
    ----------
    patterns : list[CandlestickPattern]
        Pre-sorted or unsorted list of detected patterns.
    n : int
        Number of patterns to return (default 5).

    Returns
    -------
    list[CandlestickPattern]
    """
    sorted_p = sorted(patterns, key=lambda p: p.bar_index, reverse=True)
    return sorted_p[:n]
