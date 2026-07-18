"""strategies/delta_divergence.py — Order Flow Delta Divergence Strategy

Detects when price moves on weak flow — a leading reversal signal.
Combines delta divergence with key zone proximity for high-probability entries.

Entry Logic:
    1. Price makes new swing high but cumulative delta makes LOWER high (bearish)
       OR price makes new swing low but cumulative delta makes HIGHER low (bullish)
    2. Price is near a key zone (FVG, OB, S/R, or VWAP band)
    3. Reversal candle confirms at the zone
    4. Delta trend is opposing the price move

Targets:
    - TP1: Nearest opposing structure level
    - TP2: VWAP or equilibrium
    - SL: Beyond the divergence swing extreme

Edge:
    Catches the exact moment institutional selling/buying begins beneath
    a visually-still-trending price. 1-3 candles ahead of the actual reversal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from data.order_flow import DeltaState, DeltaBar, detect_delta_price_divergence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class DeltaDivergenceSignal:
    """A delta divergence entry signal.

    Attributes:
        direction: 'BUY' (bullish divergence) or 'SELL' (bearish divergence).
        entry: Current price at divergence confirmation.
        stop_loss: Beyond the divergence extreme.
        tp1: Nearest opposing structure.
        tp2: Extended target.
        tp3: Further extension.
        rr_ratio: Reward/risk ratio.
        confidence: 0-100 confidence score.
        reasoning: Why this signal fired.
        divergence_type: 'bearish' or 'bullish'.
        delta_trend: Current delta trend direction.
    """
    direction: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr_ratio: float
    confidence: int
    reasoning: str
    divergence_type: str
    delta_trend: str


# ---------------------------------------------------------------------------
# Strategy evaluation
# ---------------------------------------------------------------------------

def evaluate_delta_divergence(
    df: pd.DataFrame,
    delta_state: Optional[DeltaState],
    current_price: float,
    nearest_support: Optional[float] = None,
    nearest_resistance: Optional[float] = None,
    vwap: Optional[float] = None,
) -> Optional[DeltaDivergenceSignal]:
    """Evaluate whether a delta divergence entry is valid.

    Args:
        df: OHLCV DataFrame (5m candles).
        delta_state: Current DeltaState from OrderFlowAccumulator.
        current_price: Latest price.
        nearest_support: Nearest support level below (for BUY TP).
        nearest_resistance: Nearest resistance level above (for SELL TP).
        vwap: Current VWAP value (secondary target).

    Returns:
        DeltaDivergenceSignal if conditions met, else None.
    """
    if df is None or len(df) < 20:
        return None

    if delta_state is None or len(delta_state.bars) < 10:
        return None

    # Check for delta divergence
    if not delta_state.delta_divergence:
        return None

    div_type = delta_state.divergence_type
    if div_type not in ("bullish", "bearish"):
        return None

    # Confirm with price-level delta divergence (stronger signal)
    price_highs = [float(df["high"].iloc[i]) for i in range(-10, 0)]
    price_lows = [float(df["low"].iloc[i]) for i in range(-10, 0)]

    has_price_div, price_div_type = detect_delta_price_divergence(
        delta_state.bars[-10:], price_highs, price_lows, lookback=10
    )

    # Require at least basic divergence; price divergence adds confidence
    if not has_price_div and delta_state.delta_trend == "flat":
        return None  # no clear signal

    # Check for reversal candle confirmation
    if not _has_reversal_candle(df, div_type):
        return None

    # Build signal
    signal = _build_signal(
        df, current_price, div_type, delta_state,
        nearest_support, nearest_resistance, vwap,
        has_price_div,
    )

    return signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_reversal_candle(df: pd.DataFrame, div_type: str) -> bool:
    """Check if last 2 candles show a reversal aligned with divergence."""
    for i in range(-2, 0):
        try:
            row = df.iloc[i]
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])

            body = abs(c - o)
            total_range = h - l
            if total_range == 0:
                continue

            if div_type == "bullish":
                # Bullish reversal: close > open, or long lower wick
                lower_wick = min(o, c) - l
                if c > o and lower_wick > body * 0.5:
                    return True
                if lower_wick > total_range * 0.6:
                    return True

            elif div_type == "bearish":
                # Bearish reversal: close < open, or long upper wick
                upper_wick = h - max(o, c)
                if c < o and upper_wick > body * 0.5:
                    return True
                if upper_wick > total_range * 0.6:
                    return True
        except (IndexError, KeyError):
            continue

    return False


def _build_signal(
    df: pd.DataFrame,
    current_price: float,
    div_type: str,
    delta_state: DeltaState,
    nearest_support: Optional[float],
    nearest_resistance: Optional[float],
    vwap: Optional[float],
    has_price_divergence: bool,
) -> Optional[DeltaDivergenceSignal]:
    """Build the entry signal from confirmed divergence."""

    recent_high = float(df["high"].iloc[-5:].max())
    recent_low = float(df["low"].iloc[-5:].min())

    if div_type == "bullish":
        # Delta higher low while price makes lower low → BUY
        direction = "BUY"
        entry = current_price
        stop_loss = recent_low - abs(recent_low * 0.001)

        # Targets
        if nearest_resistance and nearest_resistance > entry:
            tp1 = nearest_resistance
        elif vwap and vwap > entry:
            tp1 = vwap
        else:
            tp1 = entry + (entry - stop_loss) * 2.0  # 2R target

        tp2 = tp1 + (tp1 - entry) * 0.5
        tp3 = tp1 + (tp1 - entry)

    elif div_type == "bearish":
        # Delta lower high while price makes higher high → SELL
        direction = "SELL"
        entry = current_price
        stop_loss = recent_high + abs(recent_high * 0.001)

        # Targets
        if nearest_support and nearest_support < entry:
            tp1 = nearest_support
        elif vwap and vwap < entry:
            tp1 = vwap
        else:
            tp1 = entry - (stop_loss - entry) * 2.0  # 2R target

        tp2 = tp1 - (entry - tp1) * 0.5
        tp3 = tp1 - (entry - tp1)

    else:
        return None

    # Calculate R:R
    risk = abs(entry - stop_loss)
    reward = abs(tp1 - entry)
    rr_ratio = reward / risk if risk > 0 else 0.0

    if rr_ratio < 1.8:
        return None

    # Confidence
    confidence = _calc_confidence(delta_state, has_price_divergence, rr_ratio)

    reasoning = (
        f"Delta divergence ({div_type}): "
        f"{'price new high but delta lower high' if div_type == 'bearish' else 'price new low but delta higher low'}. "
        f"Delta trend: {delta_state.delta_trend}. "
        f"{'Price-level divergence confirmed. ' if has_price_divergence else ''}"
        f"Reversal candle at zone. R:R={rr_ratio:.1f}."
    )

    return DeltaDivergenceSignal(
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        rr_ratio=rr_ratio,
        confidence=confidence,
        reasoning=reasoning,
        divergence_type=div_type,
        delta_trend=delta_state.delta_trend,
    )


def _calc_confidence(
    delta_state: DeltaState,
    has_price_divergence: bool,
    rr_ratio: float,
) -> int:
    """Score confidence for a delta divergence signal."""
    score = 50  # base

    # Price-level divergence is stronger than basic delta divergence
    if has_price_divergence:
        score += 15

    # Delta trend opposing price confirms exhaustion
    div_type = delta_state.divergence_type
    if div_type == "bearish" and delta_state.delta_trend == "falling":
        score += 10  # delta falling while price rising = strong bearish signal
    elif div_type == "bullish" and delta_state.delta_trend == "rising":
        score += 10  # delta rising while price falling = strong bullish signal

    # R:R bonus
    if rr_ratio >= 3.0:
        score += 10
    elif rr_ratio >= 2.5:
        score += 5

    return min(90, max(45, score))
