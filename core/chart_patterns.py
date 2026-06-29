"""
SignalForge — Chart Pattern Detection
Detects classic chart patterns from swing points: double top/bottom,
head & shoulders, wedges, flags, and triangles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.swing_points import SwingPoint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChartPattern:
    """A detected chart pattern."""
    pattern: str         # 'double_top', 'double_bottom', 'head_shoulders',
                         #  'inv_head_shoulders', 'ascending_wedge',
                         #  'descending_wedge', 'bull_flag', 'bear_flag',
                         #  'ascending_triangle', 'descending_triangle',
                         #  'symmetric_triangle'
    direction: str       # 'bullish' or 'bearish'
    bar_start: int
    bar_end: int
    timestamp_start: pd.Timestamp
    timestamp_end: pd.Timestamp
    key_level: float     # neckline or breakout level
    target: float        # projected move target
    strength: float      # 0.0 – 1.0
    description: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _swing_highs(swings: list[SwingPoint]) -> list[SwingPoint]:
    """Filter swing points to highs only."""
    return [s for s in swings if s.type == "high"]


def _swing_lows(swings: list[SwingPoint]) -> list[SwingPoint]:
    """Filter swing points to lows only."""
    return [s for s in swings if s.type == "low"]


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """Simple ATR (average true range) scalar for the dataframe."""
    if len(df) < period + 1:
        return 0.0
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))


def _pct_diff(a: float, b: float) -> float:
    """Absolute percentage difference between two values."""
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b)


def _find_between(swings: list[SwingPoint], start_idx: int, end_idx: int,
                  swing_type: str) -> list[SwingPoint]:
    """Return swing points of *swing_type* with index between start and end."""
    return [s for s in swings
            if s.type == swing_type and start_idx < s.index < end_idx]


# ---------------------------------------------------------------------------
# Double Top / Double Bottom
# ---------------------------------------------------------------------------

def detect_double_top_bottom(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    tolerance_pct: float = 0.02,
) -> list[ChartPattern]:
    """
    Two swing highs (or lows) within *tolerance_pct* of each other with a
    clear valley (or peak) in between.

    Double top:
        Neckline = valley low between the two highs.
        Target   = neckline - (top - neckline)
    Double bottom:
        Neckline = peak high between the two lows.
        Target   = neckline + (neckline - bottom)
    """
    patterns: list[ChartPattern] = []
    highs = _swing_highs(swings)
    lows  = _swing_lows(swings)

    # --- Double Tops ---
    for i in range(len(highs) - 1):
        h1, h2 = highs[i], highs[i + 1]
        if _pct_diff(h1.price, h2.price) > tolerance_pct:
            continue
        # Valley between the two highs
        valleys = _find_between(swings, h1.index, h2.index, "low")
        if not valleys:
            continue
        neckline = min(v.price for v in valleys)
        top = max(h1.price, h2.price)
        target = neckline - (top - neckline)
        span = h2.index - h1.index
        # Strength: wider patterns are generally stronger (capped)
        strength = min(0.4 + span / 100, 1.0)
        patterns.append(ChartPattern(
            pattern="double_top",
            direction="bearish",
            bar_start=h1.index,
            bar_end=h2.index,
            timestamp_start=h1.timestamp,
            timestamp_end=h2.timestamp,
            key_level=round(neckline, 8),
            target=round(target, 8),
            strength=round(strength, 4),
            description=(
                f"Double top: peaks at ${h1.price:,.2f} and ${h2.price:,.2f} "
                f"(Δ{_pct_diff(h1.price, h2.price)*100:.1f}%), "
                f"neckline ${neckline:,.2f}, target ${target:,.2f}."
            ),
        ))

    # --- Double Bottoms ---
    for i in range(len(lows) - 1):
        l1, l2 = lows[i], lows[i + 1]
        if _pct_diff(l1.price, l2.price) > tolerance_pct:
            continue
        peaks = _find_between(swings, l1.index, l2.index, "high")
        if not peaks:
            continue
        neckline = max(p.price for p in peaks)
        bottom = min(l1.price, l2.price)
        target = neckline + (neckline - bottom)
        span = l2.index - l1.index
        strength = min(0.4 + span / 100, 1.0)
        patterns.append(ChartPattern(
            pattern="double_bottom",
            direction="bullish",
            bar_start=l1.index,
            bar_end=l2.index,
            timestamp_start=l1.timestamp,
            timestamp_end=l2.timestamp,
            key_level=round(neckline, 8),
            target=round(target, 8),
            strength=round(strength, 4),
            description=(
                f"Double bottom: troughs at ${l1.price:,.2f} and "
                f"${l2.price:,.2f} (Δ{_pct_diff(l1.price, l2.price)*100:.1f}%), "
                f"neckline ${neckline:,.2f}, target ${target:,.2f}."
            ),
        ))

    return patterns


# ---------------------------------------------------------------------------
# Head & Shoulders (and Inverse)
# ---------------------------------------------------------------------------

def detect_head_shoulders(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    tolerance_pct: float = 0.03,
) -> list[ChartPattern]:
    """
    Head & Shoulders:
        Three consecutive swing highs where the middle (head) is the highest
        and the two shoulders are within *tolerance_pct* of each other.
        Neckline = average of the two intervening troughs.

    Inverse Head & Shoulders:
        Three consecutive swing lows where the middle is the lowest.
        Neckline = average of the two intervening peaks.
    """
    patterns: list[ChartPattern] = []
    highs = _swing_highs(swings)
    lows  = _swing_lows(swings)

    # --- H&S (bearish) ---
    for i in range(len(highs) - 2):
        ls, head, rs = highs[i], highs[i + 1], highs[i + 2]
        # Head must be higher than both shoulders
        if head.price <= ls.price or head.price <= rs.price:
            continue
        # Shoulders within tolerance
        if _pct_diff(ls.price, rs.price) > tolerance_pct:
            continue

        # Two troughs: between LS–Head and Head–RS
        trough1 = _find_between(swings, ls.index, head.index, "low")
        trough2 = _find_between(swings, head.index, rs.index, "low")
        if not trough1 or not trough2:
            continue

        t1_price = min(t.price for t in trough1)
        t2_price = min(t.price for t in trough2)
        neckline = (t1_price + t2_price) / 2.0
        target = neckline - (head.price - neckline)
        span = rs.index - ls.index
        strength = min(0.5 + span / 120, 1.0)

        patterns.append(ChartPattern(
            pattern="head_shoulders",
            direction="bearish",
            bar_start=ls.index,
            bar_end=rs.index,
            timestamp_start=ls.timestamp,
            timestamp_end=rs.timestamp,
            key_level=round(neckline, 8),
            target=round(target, 8),
            strength=round(strength, 4),
            description=(
                f"Head & Shoulders: LS ${ls.price:,.2f}, Head ${head.price:,.2f}, "
                f"RS ${rs.price:,.2f}, neckline ${neckline:,.2f}, target ${target:,.2f}."
            ),
        ))

    # --- Inverse H&S (bullish) ---
    for i in range(len(lows) - 2):
        ls, head, rs = lows[i], lows[i + 1], lows[i + 2]
        if head.price >= ls.price or head.price >= rs.price:
            continue
        if _pct_diff(ls.price, rs.price) > tolerance_pct:
            continue

        peak1 = _find_between(swings, ls.index, head.index, "high")
        peak2 = _find_between(swings, head.index, rs.index, "high")
        if not peak1 or not peak2:
            continue

        p1_price = max(p.price for p in peak1)
        p2_price = max(p.price for p in peak2)
        neckline = (p1_price + p2_price) / 2.0
        target = neckline + (neckline - head.price)
        span = rs.index - ls.index
        strength = min(0.5 + span / 120, 1.0)

        patterns.append(ChartPattern(
            pattern="inv_head_shoulders",
            direction="bullish",
            bar_start=ls.index,
            bar_end=rs.index,
            timestamp_start=ls.timestamp,
            timestamp_end=rs.timestamp,
            key_level=round(neckline, 8),
            target=round(target, 8),
            strength=round(strength, 4),
            description=(
                f"Inverse H&S: LS ${ls.price:,.2f}, Head ${head.price:,.2f}, "
                f"RS ${rs.price:,.2f}, neckline ${neckline:,.2f}, target ${target:,.2f}."
            ),
        ))

    return patterns


# ---------------------------------------------------------------------------
# Wedges
# ---------------------------------------------------------------------------

def detect_wedge(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    min_touches: int = 2,
) -> list[ChartPattern]:
    """
    Ascending wedge: rising highs AND rising lows that are converging → bearish.
    Descending wedge: falling highs AND falling lows that are converging → bullish.

    Uses np.polyfit (degree=1) on swing highs and swing lows separately.
    Requires at least *min_touches* swing highs and *min_touches* swing lows
    in the pattern window.
    """
    patterns: list[ChartPattern] = []
    highs = _swing_highs(swings)
    lows  = _swing_lows(swings)

    if len(highs) < min_touches or len(lows) < min_touches:
        return patterns

    # Slide a window of increasing sizes across the swing points
    for window_size in range(min_touches * 2, len(swings) + 1):
        for start in range(0, len(swings) - window_size + 1):
            chunk = swings[start:start + window_size]
            ch = [s for s in chunk if s.type == "high"]
            cl = [s for s in chunk if s.type == "low"]

            if len(ch) < min_touches or len(cl) < min_touches:
                continue

            # Fit linear regressions on indices and prices
            h_x = np.array([s.index for s in ch], dtype=float)
            h_y = np.array([s.price for s in ch], dtype=float)
            l_x = np.array([s.index for s in cl], dtype=float)
            l_y = np.array([s.price for s in cl], dtype=float)

            try:
                h_slope, h_int = np.polyfit(h_x, h_y, 1)
                l_slope, l_int = np.polyfit(l_x, l_y, 1)
            except (np.linalg.LinAlgError, ValueError):
                continue

            # Both slopes must be the same sign (both rising or both falling)
            if h_slope * l_slope <= 0:
                continue

            # Converging: the high slope must be less steep than the low slope
            # (for rising) OR more steep for falling, i.e., abs(h_slope) < abs(l_slope)
            # Actually: converging means spread is shrinking.
            bar_start_idx = chunk[0].index
            bar_end_idx   = chunk[-1].index
            spread_start = (h_int + h_slope * bar_start_idx) - (l_int + l_slope * bar_start_idx)
            spread_end   = (h_int + h_slope * bar_end_idx)   - (l_int + l_slope * bar_end_idx)

            if spread_end >= spread_start:
                continue  # not converging

            if spread_start <= 0:
                continue  # degenerate

            convergence_rate = 1.0 - (spread_end / spread_start)

            if convergence_rate < 0.1:
                continue  # barely converging

            if h_slope > 0 and l_slope > 0:
                pattern_name = "ascending_wedge"
                direction = "bearish"
            elif h_slope < 0 and l_slope < 0:
                pattern_name = "descending_wedge"
                direction = "bullish"
            else:
                continue

            key_level = float(l_int + l_slope * bar_end_idx) if direction == "bearish" else float(h_int + h_slope * bar_end_idx)
            height = spread_start
            target = (key_level - height) if direction == "bearish" else (key_level + height)
            strength = min(0.3 + convergence_rate * 0.5 + len(chunk) / 30, 1.0)

            patterns.append(ChartPattern(
                pattern=pattern_name,
                direction=direction,
                bar_start=bar_start_idx,
                bar_end=bar_end_idx,
                timestamp_start=chunk[0].timestamp,
                timestamp_end=chunk[-1].timestamp,
                key_level=round(key_level, 8),
                target=round(target, 8),
                strength=round(strength, 4),
                description=(
                    f"{pattern_name.replace('_', ' ').title()}: bars {bar_start_idx}-"
                    f"{bar_end_idx}, convergence {convergence_rate*100:.1f}%, "
                    f"key level ${key_level:,.2f}, target ${target:,.2f}."
                ),
            ))

        # Stop after first window size that yielded results to avoid flooding
        if patterns and any(p.pattern.endswith("wedge") for p in patterns):
            break

    return patterns


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def detect_flag(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    min_pole_atr_mult: float = 3.0,
) -> list[ChartPattern]:
    """
    Bull flag: strong impulsive bullish move (pole > 3×ATR) followed by a
    short consolidation (5–20 bars) pulling back mildly.

    Bear flag: mirror image with a bearish pole.
    """
    patterns: list[ChartPattern] = []
    n = len(df)
    if n < 25:
        return patterns

    atr = _atr(df)
    if atr == 0:
        return patterns

    closes = df["close"].values
    highs_arr = df["high"].values
    lows_arr  = df["low"].values

    # Scan for impulsive moves (poles)
    for pole_len in range(5, min(40, n - 20)):
        for start in range(0, n - pole_len - 5):
            pole_end = start + pole_len
            move = closes[pole_end] - closes[start]

            if abs(move) < min_pole_atr_mult * atr:
                continue

            bullish_pole = move > 0

            # Look for consolidation after the pole (5–20 bars)
            max_flag_len = min(20, n - pole_end - 1)
            if max_flag_len < 5:
                continue

            flag_end = pole_end + max_flag_len
            flag_highs = highs_arr[pole_end:flag_end + 1]
            flag_lows  = lows_arr[pole_end:flag_end + 1]
            flag_range = np.max(flag_highs) - np.min(flag_lows)

            # Flag consolidation should be less than 50% of the pole
            if flag_range > abs(move) * 0.5:
                continue

            # Check that flag pulls back against pole direction
            flag_move = closes[flag_end] - closes[pole_end]
            if bullish_pole and flag_move > 0:
                continue  # should pull back (flat or down)
            if not bullish_pole and flag_move < 0:
                continue  # should pull back (flat or up)

            if bullish_pole:
                pattern_name = "bull_flag"
                direction = "bullish"
                key_level = float(np.max(flag_highs))  # breakout level
                target = key_level + abs(move)          # measured move
            else:
                pattern_name = "bear_flag"
                direction = "bearish"
                key_level = float(np.min(flag_lows))
                target = key_level - abs(move)

            strength = min(0.5 + (abs(move) / atr) / 20, 1.0)

            patterns.append(ChartPattern(
                pattern=pattern_name,
                direction=direction,
                bar_start=start,
                bar_end=flag_end,
                timestamp_start=df.index[start],
                timestamp_end=df.index[flag_end],
                key_level=round(key_level, 8),
                target=round(target, 8),
                strength=round(strength, 4),
                description=(
                    f"{pattern_name.replace('_', ' ').title()}: pole bars {start}-"
                    f"{pole_end} (move ${move:,.2f}), flag bars {pole_end}-{flag_end}, "
                    f"breakout ${key_level:,.2f}, target ${target:,.2f}."
                ),
            ))

        # Return after first pole_len that yields results (avoid explosion)
        if patterns and any(p.pattern.endswith("flag") for p in patterns):
            break

    return patterns


# ---------------------------------------------------------------------------
# Triangles
# ---------------------------------------------------------------------------

def detect_triangle(
    df: pd.DataFrame,
    swings: list[SwingPoint],
) -> list[ChartPattern]:
    """
    Ascending triangle:  flat resistance + rising support → bullish.
    Descending triangle: falling resistance + flat support → bearish.
    Symmetric triangle:  converging trendlines of similar absolute slope.
    """
    patterns: list[ChartPattern] = []
    highs = _swing_highs(swings)
    lows  = _swing_lows(swings)

    if len(highs) < 2 or len(lows) < 2:
        return patterns

    # Use the last several swing points for a single best-fit
    for count in range(max(2, len(highs) - 4), len(highs) + 1):
        ch = highs[-count:]
        # Need matching lows in the same window
        bar_lo = ch[0].index
        bar_hi = ch[-1].index
        cl = [s for s in lows if bar_lo <= s.index <= bar_hi]
        if len(cl) < 2:
            continue

        h_x = np.array([s.index for s in ch], dtype=float)
        h_y = np.array([s.price for s in ch], dtype=float)
        l_x = np.array([s.index for s in cl], dtype=float)
        l_y = np.array([s.price for s in cl], dtype=float)

        try:
            h_slope, h_int = np.polyfit(h_x, h_y, 1)
            l_slope, l_int = np.polyfit(l_x, l_y, 1)
        except (np.linalg.LinAlgError, ValueError):
            continue

        # Flatness threshold: slope relative to average price
        avg_price = (np.mean(h_y) + np.mean(l_y)) / 2.0
        if avg_price == 0:
            continue
        h_slope_pct = abs(h_slope) / avg_price * 100  # per bar
        l_slope_pct = abs(l_slope) / avg_price * 100

        FLAT_THRESHOLD = 0.005  # per-bar percentage slope threshold

        bar_start = ch[0].index
        bar_end = ch[-1].index
        ts_start = ch[0].timestamp
        ts_end = ch[-1].timestamp

        pattern_name = None
        direction = None

        if h_slope_pct < FLAT_THRESHOLD and l_slope > 0:
            # Flat resistance, rising support → ascending triangle
            pattern_name = "ascending_triangle"
            direction = "bullish"
        elif l_slope_pct < FLAT_THRESHOLD and h_slope < 0:
            # Flat support, falling resistance → descending triangle
            pattern_name = "descending_triangle"
            direction = "bearish"
        elif h_slope < 0 and l_slope > 0:
            # Both converging → symmetric triangle
            # Check slope magnitudes are somewhat similar
            ratio = abs(h_slope) / abs(l_slope) if abs(l_slope) > 0 else float("inf")
            if 0.3 < ratio < 3.0:
                pattern_name = "symmetric_triangle"
                direction = "bullish"  # neutral until breakout; default bullish

        if pattern_name is None:
            continue

        key_level = float(h_int + h_slope * bar_end) if direction == "bearish" else float(h_int + h_slope * bar_end)
        support_level = float(l_int + l_slope * bar_end)
        height = abs(key_level - support_level)
        target = (key_level + height) if direction == "bullish" else (support_level - height)
        strength = min(0.4 + len(ch) / 10 + len(cl) / 10, 1.0)

        patterns.append(ChartPattern(
            pattern=pattern_name,
            direction=direction,
            bar_start=bar_start,
            bar_end=bar_end,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
            key_level=round(key_level, 8),
            target=round(target, 8),
            strength=round(strength, 4),
            description=(
                f"{pattern_name.replace('_', ' ').title()}: bars {bar_start}-"
                f"{bar_end}, resistance slope {h_slope:.4f}, support slope "
                f"{l_slope:.4f}, key level ${key_level:,.2f}, target ${target:,.2f}."
            ),
        ))

        break  # one triangle detection per call is sufficient

    return patterns


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def detect_all_chart_patterns(
    df: pd.DataFrame,
    swings: list[SwingPoint],
) -> list[ChartPattern]:
    """
    Run every chart pattern detector and return the combined list sorted by
    bar_end descending (most recent first).
    """
    all_patterns: list[ChartPattern] = []
    all_patterns.extend(detect_double_top_bottom(df, swings))
    all_patterns.extend(detect_head_shoulders(df, swings))
    all_patterns.extend(detect_wedge(df, swings))
    all_patterns.extend(detect_flag(df, swings))
    all_patterns.extend(detect_triangle(df, swings))
    all_patterns.sort(key=lambda p: p.bar_end, reverse=True)
    return all_patterns


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def chart_patterns_to_dict(patterns: list[ChartPattern]) -> list[dict]:
    """Convert a list of ChartPattern instances to JSON-serialisable dicts."""
    return [
        {
            "pattern":         p.pattern,
            "direction":       p.direction,
            "bar_start":       p.bar_start,
            "bar_end":         p.bar_end,
            "timestamp_start": str(p.timestamp_start),
            "timestamp_end":   str(p.timestamp_end),
            "key_level":       p.key_level,
            "target":          p.target,
            "strength":        p.strength,
            "description":     p.description,
        }
        for p in patterns
    ]
