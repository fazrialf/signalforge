"""
SignalForge — Volume Analysis
Volume-based signal detection: climax, absorption, RVOL surges,
OBV divergence, and low-volume pullbacks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class VolumeSignal:
    """A detected volume-based signal."""
    type: str            # 'climax' | 'absorption' | 'rvol_surge' |
                         #  'obv_divergence' | 'low_volume_pullback'
    bar_index: int
    timestamp: pd.Timestamp
    direction: str       # 'bullish' or 'bearish'
    strength: float      # 0.0 – 1.0
    rvol: float          # relative volume at detection
    description: str


# ---------------------------------------------------------------------------
# Core computations
# ---------------------------------------------------------------------------

def calc_rvol(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Relative Volume = current volume / rolling mean volume over *period* bars.

    Returns a Series aligned with df.index.  Early bars (< period) are NaN.
    """
    if len(df) < 2:
        return pd.Series(np.nan, index=df.index, name="rvol")

    vol_ma = df["volume"].rolling(window=period, min_periods=period).mean()
    rvol = df["volume"] / vol_ma
    return rvol.rename("rvol")


def calc_obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume (OBV).

    Standard definition:
      OBV[i] = OBV[i-1] + volume[i]  if close[i] > close[i-1]
             = OBV[i-1] - volume[i]  if close[i] < close[i-1]
             = OBV[i-1]              if close[i] == close[i-1]
    """
    if len(df) < 2:
        return pd.Series(0.0, index=df.index, name="obv")

    close_diff = df["close"].diff()
    # +volume when price up, -volume when price down, 0 on unchanged
    signed_vol = np.where(
        close_diff > 0,  df["volume"],
        np.where(close_diff < 0, -df["volume"], 0.0)
    )
    obv = pd.Series(signed_vol, index=df.index).cumsum()
    obv.iloc[0] = 0.0  # anchor the first bar
    return obv.rename("obv")


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_volume_climax(
    df: pd.DataFrame,
    rvol_threshold: float = 3.0,
) -> list[VolumeSignal]:
    """
    Volume climax: extremely high RVOL (> *rvol_threshold*) AND close is in
    the top 20 % (bullish) or bottom 20 % (bearish) of the bar's range.

    A bullish climax suggests buying exhaustion has been absorbed and a
    reversal or continuation upward may follow.  A bearish climax is the
    mirror image.
    """
    signals: list[VolumeSignal] = []
    if len(df) < 21:
        return signals

    rvol = calc_rvol(df)
    bar_range = df["high"] - df["low"]

    for i in range(1, len(df)):
        rv = rvol.iloc[i]
        if np.isnan(rv) or rv < rvol_threshold:
            continue

        rng = bar_range.iloc[i]
        if rng == 0:
            continue

        # Position of close within the bar's range (0 = low, 1 = high)
        close_position = (df["close"].iloc[i] - df["low"].iloc[i]) / rng

        if close_position >= 0.80:          # top 20 % → bullish climax
            direction = "bullish"
            desc = (
                f"Bullish volume climax at bar {i}: RVOL={rv:.2f}x, "
                f"close in top {(close_position*100):.0f}% of range."
            )
        elif close_position <= 0.20:        # bottom 20 % → bearish climax
            direction = "bearish"
            desc = (
                f"Bearish volume climax at bar {i}: RVOL={rv:.2f}x, "
                f"close in bottom {((1-close_position)*100):.0f}% of range."
            )
        else:
            continue

        # Normalise strength: scale RVOL relative to threshold (capped at 1.0)
        strength = min((rv - rvol_threshold) / rvol_threshold + 0.5, 1.0)

        signals.append(VolumeSignal(
            type="climax",
            bar_index=i,
            timestamp=df.index[i],
            direction=direction,
            strength=round(strength, 4),
            rvol=round(float(rv), 4),
            description=desc,
        ))

    return signals


def detect_volume_absorption(
    df: pd.DataFrame,
    rvol_threshold: float = 2.0,
    max_body_pct: float = 0.3,
) -> list[VolumeSignal]:
    """
    Volume absorption: high RVOL but a small candle body (< *max_body_pct*
    fraction of the full range).

    This suggests one side is absorbing the other's aggression without
    moving price much.
      - close >= open → bullish absorption (buyers soaking up sellers)
      - close <  open → bearish absorption (sellers soaking up buyers)
    """
    signals: list[VolumeSignal] = []
    if len(df) < 21:
        return signals

    rvol = calc_rvol(df)
    bar_range = df["high"] - df["low"]

    for i in range(1, len(df)):
        rv = rvol.iloc[i]
        if np.isnan(rv) or rv < rvol_threshold:
            continue

        rng = bar_range.iloc[i]
        if rng == 0:
            continue

        body = abs(df["close"].iloc[i] - df["open"].iloc[i])
        body_pct = body / rng

        if body_pct >= max_body_pct:
            continue

        direction = "bullish" if df["close"].iloc[i] >= df["open"].iloc[i] else "bearish"
        strength = min((rv - rvol_threshold) / rvol_threshold * 0.5 + 0.5, 1.0)
        desc = (
            f"{direction.capitalize()} absorption at bar {i}: RVOL={rv:.2f}x, "
            f"body={body_pct*100:.1f}% of range (threshold {max_body_pct*100:.0f}%)."
        )

        signals.append(VolumeSignal(
            type="absorption",
            bar_index=i,
            timestamp=df.index[i],
            direction=direction,
            strength=round(strength, 4),
            rvol=round(float(rv), 4),
            description=desc,
        ))

    return signals


def detect_rvol_surge(
    df: pd.DataFrame,
    threshold: float = 2.0,
) -> list[VolumeSignal]:
    """
    Flag every bar where RVOL exceeds *threshold*.
    Direction is taken from candle colour (close vs open).
    """
    signals: list[VolumeSignal] = []
    if len(df) < 21:
        return signals

    rvol = calc_rvol(df)

    for i in range(1, len(df)):
        rv = rvol.iloc[i]
        if np.isnan(rv) or rv < threshold:
            continue

        direction = "bullish" if df["close"].iloc[i] >= df["open"].iloc[i] else "bearish"
        # Strength: how far above threshold, capped at 1.0
        strength = min((rv - threshold) / threshold + 0.5, 1.0)
        desc = (
            f"{direction.capitalize()} RVOL surge at bar {i}: "
            f"RVOL={rv:.2f}x (threshold {threshold}x)."
        )

        signals.append(VolumeSignal(
            type="rvol_surge",
            bar_index=i,
            timestamp=df.index[i],
            direction=direction,
            strength=round(strength, 4),
            rvol=round(float(rv), 4),
            description=desc,
        ))

    return signals


def detect_obv_divergence(
    df: pd.DataFrame,
    lookback: int = 20,
) -> list[VolumeSignal]:
    """
    OBV divergence over a rolling *lookback* window.

    Bearish divergence: price makes a new high in the window but OBV does not.
    Bullish divergence: price makes a new low  in the window but OBV does not.

    The signal is emitted at the final bar of each window where divergence is
    confirmed.  Consecutive identical signals are de-duplicated so only the
    first bar of a sustained divergence is flagged.
    """
    signals: list[VolumeSignal] = []
    n = len(df)
    if n < lookback + 2:
        return signals

    rvol = calc_rvol(df)
    obv = calc_obv(df)
    closes = df["close"].values
    obv_vals = obv.values

    last_bear_bar = -1
    last_bull_bar = -1

    for i in range(lookback, n):
        window_slice = slice(i - lookback, i + 1)
        price_window = closes[window_slice]
        obv_window   = obv_vals[window_slice]

        price_max_idx = int(np.argmax(price_window))
        price_min_idx = int(np.argmin(price_window))
        obv_max_idx   = int(np.argmax(obv_window))
        obv_min_idx   = int(np.argmin(obv_window))

        rv = float(rvol.iloc[i]) if not np.isnan(rvol.iloc[i]) else 1.0

        # Bearish: price high is at a later bar than OBV high → OBV lags price
        if price_max_idx > obv_max_idx and i - last_bear_bar > lookback // 2:
            last_bear_bar = i
            strength = min(0.4 + 0.6 * (price_max_idx - obv_max_idx) / lookback, 1.0)
            desc = (
                f"Bearish OBV divergence at bar {i}: price made new high at "
                f"window offset {price_max_idx} but OBV peaked at {obv_max_idx}."
            )
            signals.append(VolumeSignal(
                type="obv_divergence",
                bar_index=i,
                timestamp=df.index[i],
                direction="bearish",
                strength=round(strength, 4),
                rvol=round(rv, 4),
                description=desc,
            ))

        # Bullish: price low is at a later bar than OBV low → OBV lags price
        elif price_min_idx > obv_min_idx and i - last_bull_bar > lookback // 2:
            last_bull_bar = i
            strength = min(0.4 + 0.6 * (price_min_idx - obv_min_idx) / lookback, 1.0)
            desc = (
                f"Bullish OBV divergence at bar {i}: price made new low at "
                f"window offset {price_min_idx} but OBV troughed at {obv_min_idx}."
            )
            signals.append(VolumeSignal(
                type="obv_divergence",
                bar_index=i,
                timestamp=df.index[i],
                direction="bullish",
                strength=round(strength, 4),
                rvol=round(rv, 4),
                description=desc,
            ))

    return signals


def detect_low_volume_pullback(
    df: pd.DataFrame,
    rvol_threshold: float = 0.5,
    lookback: int = 5,
) -> list[VolumeSignal]:
    """
    Low-volume pullback: a series of bars pulling back against the prior trend
    while RVOL stays below *rvol_threshold* (< 0.5 by default).

    Prior trend is established when at least 3 of the *lookback* bars
    preceding the current bar are directionally consistent (bullish trend:
    3+ up-closes; bearish trend: 3+ down-closes).

    A low-volume pullback in an uptrend is bullish (expect resumption);
    in a downtrend it is bearish.
    """
    signals: list[VolumeSignal] = []
    n = len(df)
    if n < lookback + 21:   # need enough history for both RVOL and trend check
        return signals

    rvol = calc_rvol(df)
    closes = df["close"].values
    opens  = df["open"].values

    for i in range(lookback + 20, n):
        rv = float(rvol.iloc[i])
        if np.isnan(rv) or rv >= rvol_threshold:
            continue

        # Determine direction of each bar in the prior *lookback* window
        prior = range(i - lookback, i)
        up_bars   = sum(1 for j in prior if closes[j] > opens[j])
        down_bars = sum(1 for j in prior if closes[j] < opens[j])

        if up_bars >= 3:          # established uptrend → bullish signal
            trend = "bullish"
        elif down_bars >= 3:      # established downtrend → bearish signal
            trend = "bearish"
        else:
            continue

        # Confirm current bar is actually pulling back against the trend
        current_up = closes[i] > opens[i]
        if trend == "bullish" and current_up:
            continue   # not a pullback, still advancing
        if trend == "bearish" and not current_up:
            continue   # not a pullback, still declining

        strength = min((rvol_threshold - rv) / rvol_threshold * 0.8 + 0.2, 1.0)
        desc = (
            f"{trend.capitalize()} low-volume pullback at bar {i}: "
            f"RVOL={rv:.2f}x (below {rvol_threshold}x), "
            f"prior {lookback}-bar trend={'up' if trend=='bullish' else 'down'} "
            f"({up_bars} up / {down_bars} down bars)."
        )

        signals.append(VolumeSignal(
            type="low_volume_pullback",
            bar_index=i,
            timestamp=df.index[i],
            direction=trend,
            strength=round(strength, 4),
            rvol=round(rv, 4),
            description=desc,
        ))

    return signals


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def detect_all_volume_signals(df: pd.DataFrame) -> list[VolumeSignal]:
    """
    Run every volume detector and return the combined list sorted by
    bar_index descending (most recent first).
    """
    all_signals: list[VolumeSignal] = []
    all_signals.extend(detect_volume_climax(df))
    all_signals.extend(detect_volume_absorption(df))
    all_signals.extend(detect_rvol_surge(df))
    all_signals.extend(detect_obv_divergence(df))
    all_signals.extend(detect_low_volume_pullback(df))
    all_signals.sort(key=lambda s: s.bar_index, reverse=True)
    return all_signals


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def volume_signals_to_dict(signals: list[VolumeSignal]) -> list[dict]:
    """Convert a list of VolumeSignal instances to JSON-serialisable dicts."""
    return [
        {
            "type":        s.type,
            "bar_index":   s.bar_index,
            "timestamp":   str(s.timestamp),
            "direction":   s.direction,
            "strength":    s.strength,
            "rvol":        s.rvol,
            "description": s.description,
        }
        for s in signals
    ]
