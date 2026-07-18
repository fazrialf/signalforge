"""core/vwap.py — Session VWAP + Standard Deviation Bands

Calculates Volume-Weighted Average Price anchored to trading session starts.
Used by the VWAP Mean Reversion strategy for fade entries at ±2σ bands.

VWAP Formula:
    VWAP = Σ(typical_price × volume) / Σ(volume)
    typical_price = (high + low + close) / 3

Band Calculation:
    variance = Σ(volume × (typical_price - VWAP)²) / Σ(volume)
    std_dev = sqrt(variance)
    upper_band_N = VWAP + N × std_dev
    lower_band_N = VWAP - N × std_dev

Session anchoring:
    VWAP resets at midnight UTC (daily session) by default.
    Can also be anchored to London open (07:00 UTC) or NY open (13:00 UTC).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class VWAPState:
    """Current VWAP state for one asset.

    Attributes:
        vwap: Current VWAP value.
        upper_1: VWAP + 1σ band.
        lower_1: VWAP - 1σ band.
        upper_2: VWAP + 2σ band.
        lower_2: VWAP - 2σ band.
        upper_3: VWAP + 3σ band (extreme).
        lower_3: VWAP - 3σ band (extreme).
        std_dev: Current standard deviation.
        price_vs_vwap: Current price distance from VWAP as % (positive = above).
        band_position: Which band zone price is in (-3 to +3, 0 = at VWAP).
        bars_in_session: How many bars since session anchor.
    """
    vwap: float
    upper_1: float
    lower_1: float
    upper_2: float
    lower_2: float
    upper_3: float
    lower_3: float
    std_dev: float
    price_vs_vwap: float
    band_position: int  # -3, -2, -1, 0, +1, +2, +3
    bars_in_session: int

    def to_dict(self) -> dict:
        return {
            "vwap": round(self.vwap, 6),
            "upper_1": round(self.upper_1, 6),
            "lower_1": round(self.lower_1, 6),
            "upper_2": round(self.upper_2, 6),
            "lower_2": round(self.lower_2, 6),
            "upper_3": round(self.upper_3, 6),
            "lower_3": round(self.lower_3, 6),
            "std_dev": round(self.std_dev, 6),
            "price_vs_vwap": round(self.price_vs_vwap, 4),
            "band_position": self.band_position,
            "bars_in_session": self.bars_in_session,
        }


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def calc_vwap(
    df: pd.DataFrame,
    anchor_hour_utc: int = 0,
) -> Optional[VWAPState]:
    """Calculate session-anchored VWAP with standard deviation bands.

    Args:
        df: OHLCV DataFrame with columns: open, high, low, close, volume.
            Index should be datetime or timestamps.
        anchor_hour_utc: Hour (0-23 UTC) at which VWAP resets.
            0 = midnight (daily), 7 = London open, 13 = NY open.

    Returns:
        VWAPState with current values, or None if insufficient data.
    """
    if df is None or len(df) < 5:
        return None

    try:
        high = df["high"].astype(float).values
        low = df["low"].astype(float).values
        close = df["close"].astype(float).values
        volume = df["volume"].astype(float).values

        # Find session anchor — last occurrence of anchor_hour in the data
        # For intraday (5m) data, find the bar closest to the anchor hour
        session_start_idx = _find_session_anchor(df, anchor_hour_utc)

        # Slice to current session only
        high = high[session_start_idx:]
        low = low[session_start_idx:]
        close = close[session_start_idx:]
        volume = volume[session_start_idx:]

        if len(close) < 3 or volume.sum() == 0:
            return None

        # Typical price
        typical_price = (high + low + close) / 3.0

        # Cumulative sums for VWAP
        cum_tp_vol = np.cumsum(typical_price * volume)
        cum_vol = np.cumsum(volume)

        # Avoid division by zero
        cum_vol_safe = np.where(cum_vol == 0, 1.0, cum_vol)
        vwap_array = cum_tp_vol / cum_vol_safe

        # Standard deviation bands (rolling variance)
        # variance = Σ(vol × (tp - vwap)²) / Σ(vol)
        squared_dev = (typical_price - vwap_array) ** 2
        cum_var = np.cumsum(squared_dev * volume) / cum_vol_safe
        std_array = np.sqrt(cum_var)

        # Current values (last bar)
        vwap = float(vwap_array[-1])
        std = float(std_array[-1])
        current_price = float(close[-1])

        if vwap == 0:
            return None

        # Band values
        upper_1 = vwap + std
        lower_1 = vwap - std
        upper_2 = vwap + 2 * std
        lower_2 = vwap - 2 * std
        upper_3 = vwap + 3 * std
        lower_3 = vwap - 3 * std

        # Price vs VWAP as percentage
        price_vs_vwap = ((current_price - vwap) / vwap) * 100

        # Determine band position
        band_position = _get_band_position(current_price, vwap, std)

        return VWAPState(
            vwap=vwap,
            upper_1=upper_1,
            lower_1=lower_1,
            upper_2=upper_2,
            lower_2=lower_2,
            upper_3=upper_3,
            lower_3=lower_3,
            std_dev=std,
            price_vs_vwap=price_vs_vwap,
            band_position=band_position,
            bars_in_session=len(close),
        )

    except Exception as e:
        logger.warning("[VWAP] Calculation failed: %s", e)
        return None


def calc_vwap_series(
    df: pd.DataFrame,
    anchor_hour_utc: int = 0,
) -> Optional[pd.DataFrame]:
    """Calculate full VWAP + bands as a DataFrame (for backtesting/charting).

    Returns DataFrame with columns: vwap, upper_1, lower_1, upper_2, lower_2, std.
    Only contains rows from the current session anchor onwards.
    """
    if df is None or len(df) < 5:
        return None

    try:
        session_start_idx = _find_session_anchor(df, anchor_hour_utc)
        session_df = df.iloc[session_start_idx:].copy()

        if len(session_df) < 3:
            return None

        high = session_df["high"].astype(float).values
        low = session_df["low"].astype(float).values
        close = session_df["close"].astype(float).values
        volume = session_df["volume"].astype(float).values

        if volume.sum() == 0:
            return None

        typical_price = (high + low + close) / 3.0
        cum_tp_vol = np.cumsum(typical_price * volume)
        cum_vol = np.cumsum(volume)
        cum_vol_safe = np.where(cum_vol == 0, 1.0, cum_vol)

        vwap_array = cum_tp_vol / cum_vol_safe
        squared_dev = (typical_price - vwap_array) ** 2
        cum_var = np.cumsum(squared_dev * volume) / cum_vol_safe
        std_array = np.sqrt(cum_var)

        result = pd.DataFrame({
            "vwap": vwap_array,
            "upper_1": vwap_array + std_array,
            "lower_1": vwap_array - std_array,
            "upper_2": vwap_array + 2 * std_array,
            "lower_2": vwap_array - 2 * std_array,
            "std": std_array,
        }, index=session_df.index)

        return result

    except Exception as e:
        logger.warning("[VWAP] Series calculation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_session_anchor(df: pd.DataFrame, anchor_hour_utc: int) -> int:
    """Find the index of the most recent session anchor in the DataFrame.

    Walks backward from the end to find the first bar at or after anchor_hour
    on the most recent day that contains it. Falls back to using the last
    24 hours of data if no clean anchor is found.
    """
    try:
        index = df.index
        if hasattr(index, 'hour'):
            # DatetimeIndex — find the last occurrence of anchor hour
            hours = index.hour
            # Find bars at the anchor hour
            anchor_mask = hours == anchor_hour_utc

            if anchor_mask.any():
                # Get the last anchor point
                last_anchor_pos = np.where(anchor_mask)[0][-1]
                return int(last_anchor_pos)

        # Fallback: if index isn't datetime or no anchor found,
        # use last 288 bars (24h of 5m data)
        max_session_bars = 288
        return max(0, len(df) - max_session_bars)

    except Exception:
        # Ultimate fallback — use last 288 bars
        return max(0, len(df) - 288)


def _get_band_position(price: float, vwap: float, std: float) -> int:
    """Determine which VWAP band zone the price is in.

    Returns:
        -3: below lower 3σ (extreme oversold)
        -2: between lower 2σ and 3σ (extended — fade zone)
        -1: between lower 1σ and 2σ
         0: between ±1σ (neutral / at VWAP)
        +1: between upper 1σ and 2σ
        +2: between upper 2σ and 3σ (extended — fade zone)
        +3: above upper 3σ (extreme overbought)
    """
    if std <= 0:
        return 0

    distance = (price - vwap) / std

    if distance >= 3.0:
        return 3
    elif distance >= 2.0:
        return 2
    elif distance >= 1.0:
        return 1
    elif distance <= -3.0:
        return -3
    elif distance <= -2.0:
        return -2
    elif distance <= -1.0:
        return -1
    else:
        return 0


def is_at_fade_zone(vwap_state: Optional[VWAPState]) -> tuple[bool, str]:
    """Check if price is at a VWAP fade zone (±2σ or beyond).

    Returns:
        (is_fade_zone, direction)
        direction: 'bullish' (price at lower band, fade up) or
                   'bearish' (price at upper band, fade down) or ''
    """
    if vwap_state is None:
        return False, ""

    if vwap_state.band_position >= 2:
        return True, "bearish"  # price extended above — fade short
    elif vwap_state.band_position <= -2:
        return True, "bullish"  # price extended below — fade long
    return False, ""
