"""strategies/vwap_reversion.py — VWAP Mean Reversion Strategy

Fades extended moves back to VWAP when price reaches ±2σ bands.
Best during ranging markets (London open, early NY session).

Entry Logic:
    1. Price extends to ±2σ VWAP band (or beyond)
    2. Rejection candle forms at the band (wick > body, closes back inside)
    3. EMA9 slope confirms momentum is fading (not accelerating)
    4. NOT during a squeeze firing (avoid fading breakouts)

Targets:
    - TP1: VWAP (mean reversion target)
    - TP2: Opposite ±1σ band
    - SL: Beyond the rejection wick (tight — the band should hold)

Risk:
    - R:R typically 2.0–3.0 (band edge to mean vs wick SL)
    - Avoid during news events (bands get blown through)
    - Avoid when ATR is spiking (use filter_8 to block)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from core.vwap import VWAPState, calc_vwap, is_at_fade_zone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class VWAPReversionSignal:
    """A VWAP reversion entry signal.

    Attributes:
        direction: 'BUY' (fade from lower band) or 'SELL' (fade from upper band).
        entry: Suggested entry price (rejection candle close).
        stop_loss: Beyond the wick/band extreme.
        tp1: VWAP (mean).
        tp2: Opposite ±1σ band.
        tp3: Opposite ±2σ band (extended target).
        rr_ratio: Reward/risk ratio.
        confidence: 0–100 confidence score.
        reasoning: Why this signal fired.
        vwap_state: The VWAP state at time of signal.
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
    vwap_state: VWAPState


# ---------------------------------------------------------------------------
# Strategy evaluation
# ---------------------------------------------------------------------------

def evaluate_vwap_reversion(
    df: pd.DataFrame,
    vwap_state: Optional[VWAPState] = None,
    anchor_hour_utc: int = 0,
    squeeze_firing: bool = False,
    atr_spike: bool = False,
) -> Optional[VWAPReversionSignal]:
    """Evaluate whether a VWAP mean reversion entry is valid.

    Args:
        df: OHLCV DataFrame (5m candles).
        vwap_state: Pre-computed VWAPState. If None, computed fresh.
        anchor_hour_utc: VWAP anchor hour (0=midnight, 7=London, 13=NY).
        squeeze_firing: Whether a squeeze is currently firing (avoid fading).
        atr_spike: Whether ATR is in spike regime (avoid fading).

    Returns:
        VWAPReversionSignal if conditions met, else None.
    """
    if df is None or len(df) < 20:
        return None

    # Don't fade during breakouts or volatility spikes
    if squeeze_firing:
        return None
    if atr_spike:
        return None

    # Compute VWAP if not provided
    if vwap_state is None:
        vwap_state = calc_vwap(df, anchor_hour_utc=anchor_hour_utc)

    if vwap_state is None:
        return None

    # Check if price is at a fade zone (±2σ or beyond)
    at_fade, fade_direction = is_at_fade_zone(vwap_state)
    if not at_fade:
        return None

    # Need enough bars in session for VWAP to be meaningful
    if vwap_state.bars_in_session < 20:
        return None

    # Check for rejection candle at the band
    if not _has_rejection_candle(df, fade_direction):
        return None

    # Check momentum is fading (EMA9 slope flattening/reversing)
    if not _momentum_fading(df, fade_direction):
        return None

    # Build the signal
    current_price = float(df["close"].iloc[-1])
    candle_high = float(df["high"].iloc[-1])
    candle_low = float(df["low"].iloc[-1])

    if fade_direction == "bullish":
        # Price at lower band — fade UP (BUY)
        entry = current_price
        stop_loss = candle_low - abs(candle_low * 0.001)  # below rejection wick
        tp1 = vwap_state.vwap
        tp2 = vwap_state.upper_1
        tp3 = vwap_state.upper_2
        direction = "BUY"
    else:
        # Price at upper band — fade DOWN (SELL)
        entry = current_price
        stop_loss = candle_high + abs(candle_high * 0.001)  # above rejection wick
        tp1 = vwap_state.vwap
        tp2 = vwap_state.lower_1
        tp3 = vwap_state.lower_2
        direction = "SELL"

    # Calculate R:R
    risk = abs(entry - stop_loss)
    reward = abs(tp1 - entry)
    rr_ratio = reward / risk if risk > 0 else 0.0

    # Minimum R:R check
    if rr_ratio < 1.8:
        return None

    # Confidence scoring
    confidence = _calc_confidence(vwap_state, rr_ratio, df)

    # Build reasoning
    band_pos = vwap_state.band_position
    reasoning = (
        f"VWAP reversion: price at {band_pos:+d}σ band "
        f"(VWAP=${vwap_state.vwap:.2f}, price deviation={vwap_state.price_vs_vwap:.2f}%). "
        f"Rejection candle confirmed with fading momentum. "
        f"Target: mean reversion to VWAP. R:R={rr_ratio:.1f}."
    )

    return VWAPReversionSignal(
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        rr_ratio=rr_ratio,
        confidence=confidence,
        reasoning=reasoning,
        vwap_state=vwap_state,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_rejection_candle(df: pd.DataFrame, direction: str) -> bool:
    """Check if the last 1-2 candles show rejection at the band.

    For bullish (fading from lower band): long lower wick, body in upper half.
    For bearish (fading from upper band): long upper wick, body in lower half.
    """
    for i in range(-2, 0):  # check last 2 candles
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

            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l

            if direction == "bullish":
                # Rejection from below: long lower wick (>50% of range)
                if lower_wick > total_range * 0.5 and body < total_range * 0.4:
                    return True
                # Or bullish close (close > open) with wick below
                if c > o and lower_wick > body:
                    return True

            elif direction == "bearish":
                # Rejection from above: long upper wick (>50% of range)
                if upper_wick > total_range * 0.5 and body < total_range * 0.4:
                    return True
                # Or bearish close (close < open) with wick above
                if c < o and upper_wick > body:
                    return True
        except (IndexError, KeyError):
            continue

    return False


def _momentum_fading(df: pd.DataFrame, direction: str) -> bool:
    """Check if EMA9 momentum is fading (not accelerating away from VWAP).

    For bullish fade: EMA9 slope should be flattening or turning up.
    For bearish fade: EMA9 slope should be flattening or turning down.
    """
    if len(df) < 12:
        return True  # not enough data — allow signal anyway

    close = df["close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()

    # Current slope (last 3 bars)
    current_slope = (ema9.iloc[-1] - ema9.iloc[-3]) / ema9.iloc[-3]
    # Previous slope (bars -5 to -3)
    prev_slope = (ema9.iloc[-3] - ema9.iloc[-5]) / ema9.iloc[-5]

    if direction == "bullish":
        # Momentum was falling, now flattening or reversing
        # (slope becoming less negative or turning positive)
        return current_slope > prev_slope or current_slope > -0.0001
    else:
        # Momentum was rising, now flattening or reversing
        return current_slope < prev_slope or current_slope < 0.0001


def _calc_confidence(
    vwap_state: VWAPState,
    rr_ratio: float,
    df: pd.DataFrame,
) -> int:
    """Calculate confidence score for the reversion signal."""
    score = 50  # base

    # Band position bonus (further = more confident in reversion)
    band = abs(vwap_state.band_position)
    if band >= 3:
        score += 20  # extreme extension
    elif band >= 2:
        score += 10  # standard fade zone

    # R:R bonus
    if rr_ratio >= 3.0:
        score += 15
    elif rr_ratio >= 2.5:
        score += 10
    elif rr_ratio >= 2.0:
        score += 5

    # Session maturity — VWAP is more reliable with more bars
    if vwap_state.bars_in_session >= 50:
        score += 10
    elif vwap_state.bars_in_session >= 30:
        score += 5

    # Volume confirmation — recent volume should be declining (exhaustion)
    try:
        vol = df["volume"].astype(float)
        recent_vol = vol.iloc[-3:].mean()
        prev_vol = vol.iloc[-6:-3].mean()
        if prev_vol > 0 and recent_vol < prev_vol * 0.8:
            score += 5  # volume declining — exhaustion confirmed
    except Exception:
        pass

    return min(95, max(40, score))
