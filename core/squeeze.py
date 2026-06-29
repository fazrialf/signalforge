"""Detect Bollinger Band + Keltner Channel squeeze (TTM Squeeze).

Implements the LazyBear TTM Squeeze logic using only pandas and numpy.
When Bollinger Bands contract inside Keltner Channels the market is
"squeezing"; a release (firing) often precedes a strong directional move.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class SqueezeState:
    """Snapshot of the squeeze condition for a single bar."""

    bar_index: int
    timestamp: pd.Timestamp
    in_squeeze: bool  # True when BB sits inside KC
    squeeze_bars: int  # consecutive bars in squeeze up to this point
    momentum: float  # histogram-like momentum value
    momentum_direction: str  # 'up', 'down', or 'flat'
    firing: bool  # squeeze just released (was in, now out)
    direction: str  # 'bullish' or 'bearish' based on momentum sign


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Compute True Range (max of three ranges)."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def _momentum_value(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    kc_period: int,
    idx: int,
) -> float:
    """Compute the squeeze momentum for bar *idx*.

    Momentum = slope of a linear regression fitted to
    (close − midpoint) over the last *kc_period* bars, where
    midpoint = (highest_high + lowest_low) / 2 over that window.
    """
    if idx < kc_period - 1:
        return 0.0

    start = idx - kc_period + 1
    window_close = close.iloc[start : idx + 1].values
    window_high = high.iloc[start : idx + 1].values
    window_low = low.iloc[start : idx + 1].values

    # Midpoint of the price range over the window
    midpoint = (np.nanmax(window_high) + np.nanmin(window_low)) / 2.0

    # Delta series: how far close is from the midpoint each bar
    delta = window_close - midpoint

    n = len(delta)
    if n < 2:
        return 0.0

    # Linear regression slope as momentum proxy
    x = np.arange(n, dtype=float)
    # Guard against all-NaN slices
    mask = ~np.isnan(delta)
    if mask.sum() < 2:
        return 0.0

    slope = np.polyfit(x[mask], delta[mask], 1)[0]
    return float(slope)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calc_squeeze(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> list[SqueezeState]:
    """Calculate the TTM Squeeze state for every bar in *df*.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain ``close``, ``high``, and ``low`` columns.
    bb_period : int
        Bollinger Band lookback period.
    bb_std : float
        Number of standard deviations for BB.
    kc_period : int
        Keltner Channel lookback period.
    kc_mult : float
        ATR multiplier for KC width.

    Returns
    -------
    list[SqueezeState]
        One entry per bar that has enough data (first ~max(bb_period, kc_period)
        bars may be skipped).
    """
    min_period = max(bb_period, kc_period)

    # Gracefully handle short DataFrames
    if len(df) < min_period:
        return []

    # Normalise column names to lowercase for convenience
    col_map = {c: c.lower() for c in df.columns}
    data = df.rename(columns=col_map)

    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)

    # --- Bollinger Bands ---------------------------------------------------
    bb_sma = _sma(close, bb_period)
    bb_stddev = close.rolling(window=bb_period, min_periods=bb_period).std()
    bb_upper = bb_sma + bb_std * bb_stddev
    bb_lower = bb_sma - bb_std * bb_stddev

    # --- Keltner Channels --------------------------------------------------
    kc_ema = _ema(close, kc_period)
    tr = _true_range(high, low, close)
    atr = _ema(tr, kc_period)  # EMA-based ATR
    kc_upper = kc_ema + kc_mult * atr
    kc_lower = kc_ema - kc_mult * atr

    # --- Squeeze detection -------------------------------------------------
    results: list[SqueezeState] = []
    prev_in_squeeze = False
    consecutive_squeeze = 0

    # Determine timestamps
    has_dt_index = isinstance(df.index, pd.DatetimeIndex)

    for i in range(min_period - 1, len(df)):
        # Skip bars where any band value is NaN
        if (
            np.isnan(bb_upper.iloc[i])
            or np.isnan(kc_upper.iloc[i])
            or np.isnan(bb_lower.iloc[i])
            or np.isnan(kc_lower.iloc[i])
        ):
            prev_in_squeeze = False
            consecutive_squeeze = 0
            continue

        # BB inside KC → squeeze is on
        in_sq = bool(bb_upper.iloc[i] < kc_upper.iloc[i] and bb_lower.iloc[i] > kc_lower.iloc[i])

        if in_sq:
            consecutive_squeeze += 1
        else:
            consecutive_squeeze = 0

        # Firing = squeeze just released
        firing = prev_in_squeeze and not in_sq

        # Momentum for this bar
        mom = _momentum_value(close, high, low, kc_period, i)

        # Momentum direction relative to previous bar
        if len(results) > 0:
            prev_mom = results[-1].momentum
            if mom > prev_mom + 1e-12:
                mom_dir = "up"
            elif mom < prev_mom - 1e-12:
                mom_dir = "down"
            else:
                mom_dir = "flat"
        else:
            mom_dir = "flat"

        # Overall direction based on momentum sign
        direction = "bullish" if mom >= 0 else "bearish"

        # Timestamp
        if has_dt_index:
            ts = pd.Timestamp(df.index[i])
        elif "timestamp" in data.columns:
            ts = pd.Timestamp(data["timestamp"].iloc[i])
        elif "date" in data.columns:
            ts = pd.Timestamp(data["date"].iloc[i])
        else:
            ts = pd.Timestamp.now()

        results.append(SqueezeState(
            bar_index=i,
            timestamp=ts,
            in_squeeze=in_sq,
            squeeze_bars=consecutive_squeeze,
            momentum=mom,
            momentum_direction=mom_dir,
            firing=firing,
            direction=direction,
        ))

        prev_in_squeeze = in_sq

    return results


def get_current_squeeze(df: pd.DataFrame, **kwargs) -> Optional[SqueezeState]:
    """Return the SqueezeState for the last bar, or None if not enough data."""
    states = calc_squeeze(df, **kwargs)
    return states[-1] if states else None


def is_squeeze_firing(df: pd.DataFrame, **kwargs) -> bool:
    """Quick check: is the squeeze firing (releasing) on the latest bar?"""
    state = get_current_squeeze(df, **kwargs)
    return state.firing if state is not None else False


def squeeze_to_dict(state: SqueezeState) -> dict:
    """Serialise a single SqueezeState to a plain dict."""
    return asdict(state)
