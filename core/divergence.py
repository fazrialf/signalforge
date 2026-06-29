"""Detect RSI and MACD divergences from OHLCV + indicator data.

Regular divergences signal potential reversals; hidden divergences signal
trend continuation. Works with pandas DataFrames and Series only (no
external TA libraries).
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
class DivergenceSignal:
    """Single divergence event between price and an oscillator."""

    type: str  # 'bullish_regular', 'bearish_regular', 'bullish_hidden', 'bearish_hidden'
    indicator: str  # 'rsi' or 'macd'
    bar_index_1: int  # first swing point
    bar_index_2: int  # second swing point (more recent)
    price_1: float
    price_2: float
    indicator_1: float
    indicator_2: float
    strength: float  # 0.0–1.0 based on divergence magnitude
    timestamp: pd.Timestamp


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _swing_lows(series: pd.Series, lookback: int) -> list[int]:
    """Return integer-location indices of local minima within *lookback* bars."""
    lows: list[int] = []
    values = series.values
    n = len(values)
    for i in range(lookback, n - lookback):
        window = values[i - lookback : i + lookback + 1]
        if np.isnan(values[i]):
            continue
        # Current value must be the strict minimum of the window
        if values[i] == np.nanmin(window) and np.sum(window == values[i]) == 1:
            lows.append(i)
    return lows


def _swing_highs(series: pd.Series, lookback: int) -> list[int]:
    """Return integer-location indices of local maxima within *lookback* bars."""
    highs: list[int] = []
    values = series.values
    n = len(values)
    for i in range(lookback, n - lookback):
        window = values[i - lookback : i + lookback + 1]
        if np.isnan(values[i]):
            continue
        if values[i] == np.nanmax(window) and np.sum(window == values[i]) == 1:
            highs.append(i)
    return highs


def _calc_strength(
    price_1: float, price_2: float, ind_1: float, ind_2: float
) -> float:
    """Strength ∈ [0, 1] based on the mismatch between price and indicator moves.

    The bigger the disagreement between price % change and indicator % change,
    the stronger the divergence signal.
    """
    # Percent changes (guard against division by zero)
    price_pct = abs(price_2 - price_1) / max(abs(price_1), 1e-12)
    ind_pct = abs(ind_2 - ind_1) / max(abs(ind_1), 1e-12)
    raw = abs(price_pct - ind_pct)
    # Clamp to [0, 1]
    return float(np.clip(raw, 0.0, 1.0))


def _detect_divergences(
    df: pd.DataFrame,
    indicator_series: pd.Series,
    indicator_name: str,
    lookback: int = 5,
    min_bars_apart: int = 5,
    max_bars_apart: int = 50,
) -> list[DivergenceSignal]:
    """Core divergence detection shared by RSI and MACD.

    Scans swing highs/lows of both price (close) and the indicator, then
    checks the four divergence patterns.
    """
    if len(df) < lookback * 2 + 1:
        return []

    close = df["close"] if "close" in df.columns else df["Close"]
    close = close.reset_index(drop=True)
    indicator_series = indicator_series.reset_index(drop=True)

    # Drop NaN tail from indicator (common for RSI/MACD early bars)
    valid_mask = indicator_series.notna() & close.notna()
    if valid_mask.sum() < lookback * 2 + 1:
        return []

    # Find swing points on price and indicator
    price_lows = _swing_lows(close, lookback)
    price_highs = _swing_highs(close, lookback)
    ind_lows = _swing_lows(indicator_series, lookback)
    ind_highs = _swing_highs(indicator_series, lookback)

    signals: list[DivergenceSignal] = []
    timestamps = df.index if isinstance(df.index, pd.DatetimeIndex) else None

    def _ts(idx: int) -> pd.Timestamp:
        """Best-effort timestamp for bar *idx*."""
        if timestamps is not None:
            return pd.Timestamp(timestamps[idx])
        if "timestamp" in df.columns:
            return pd.Timestamp(df["timestamp"].iloc[idx])
        if "date" in df.columns:
            return pd.Timestamp(df["date"].iloc[idx])
        return pd.Timestamp.now()

    # --- Regular bullish: price lower low, indicator higher low -------------
    for j in range(1, len(price_lows)):
        idx2 = price_lows[j]
        for i in range(j - 1, -1, -1):
            idx1 = price_lows[i]
            gap = idx2 - idx1
            if gap < min_bars_apart:
                continue
            if gap > max_bars_apart:
                break
            # Price makes lower low
            if close.iloc[idx2] >= close.iloc[idx1]:
                continue
            # Find closest indicator low near idx1 and idx2
            il1 = _nearest(ind_lows, idx1, lookback)
            il2 = _nearest(ind_lows, idx2, lookback)
            if il1 is None or il2 is None:
                continue
            # Indicator makes higher low
            if indicator_series.iloc[il2] <= indicator_series.iloc[il1]:
                continue
            signals.append(DivergenceSignal(
                type="bullish_regular",
                indicator=indicator_name,
                bar_index_1=idx1,
                bar_index_2=idx2,
                price_1=float(close.iloc[idx1]),
                price_2=float(close.iloc[idx2]),
                indicator_1=float(indicator_series.iloc[il1]),
                indicator_2=float(indicator_series.iloc[il2]),
                strength=_calc_strength(
                    close.iloc[idx1], close.iloc[idx2],
                    indicator_series.iloc[il1], indicator_series.iloc[il2],
                ),
                timestamp=_ts(idx2),
            ))
            break  # take nearest match per idx2

    # --- Regular bearish: price higher high, indicator lower high -----------
    for j in range(1, len(price_highs)):
        idx2 = price_highs[j]
        for i in range(j - 1, -1, -1):
            idx1 = price_highs[i]
            gap = idx2 - idx1
            if gap < min_bars_apart:
                continue
            if gap > max_bars_apart:
                break
            if close.iloc[idx2] <= close.iloc[idx1]:
                continue
            ih1 = _nearest(ind_highs, idx1, lookback)
            ih2 = _nearest(ind_highs, idx2, lookback)
            if ih1 is None or ih2 is None:
                continue
            if indicator_series.iloc[ih2] >= indicator_series.iloc[ih1]:
                continue
            signals.append(DivergenceSignal(
                type="bearish_regular",
                indicator=indicator_name,
                bar_index_1=idx1,
                bar_index_2=idx2,
                price_1=float(close.iloc[idx1]),
                price_2=float(close.iloc[idx2]),
                indicator_1=float(indicator_series.iloc[ih1]),
                indicator_2=float(indicator_series.iloc[ih2]),
                strength=_calc_strength(
                    close.iloc[idx1], close.iloc[idx2],
                    indicator_series.iloc[ih1], indicator_series.iloc[ih2],
                ),
                timestamp=_ts(idx2),
            ))
            break

    # --- Hidden bullish: price higher low, indicator lower low --------------
    for j in range(1, len(price_lows)):
        idx2 = price_lows[j]
        for i in range(j - 1, -1, -1):
            idx1 = price_lows[i]
            gap = idx2 - idx1
            if gap < min_bars_apart:
                continue
            if gap > max_bars_apart:
                break
            if close.iloc[idx2] <= close.iloc[idx1]:
                continue
            il1 = _nearest(ind_lows, idx1, lookback)
            il2 = _nearest(ind_lows, idx2, lookback)
            if il1 is None or il2 is None:
                continue
            if indicator_series.iloc[il2] >= indicator_series.iloc[il1]:
                continue
            signals.append(DivergenceSignal(
                type="bullish_hidden",
                indicator=indicator_name,
                bar_index_1=idx1,
                bar_index_2=idx2,
                price_1=float(close.iloc[idx1]),
                price_2=float(close.iloc[idx2]),
                indicator_1=float(indicator_series.iloc[il1]),
                indicator_2=float(indicator_series.iloc[il2]),
                strength=_calc_strength(
                    close.iloc[idx1], close.iloc[idx2],
                    indicator_series.iloc[il1], indicator_series.iloc[il2],
                ),
                timestamp=_ts(idx2),
            ))
            break

    # --- Hidden bearish: price lower high, indicator higher high ------------
    for j in range(1, len(price_highs)):
        idx2 = price_highs[j]
        for i in range(j - 1, -1, -1):
            idx1 = price_highs[i]
            gap = idx2 - idx1
            if gap < min_bars_apart:
                continue
            if gap > max_bars_apart:
                break
            if close.iloc[idx2] >= close.iloc[idx1]:
                continue
            ih1 = _nearest(ind_highs, idx1, lookback)
            ih2 = _nearest(ind_highs, idx2, lookback)
            if ih1 is None or ih2 is None:
                continue
            if indicator_series.iloc[ih2] <= indicator_series.iloc[ih1]:
                continue
            signals.append(DivergenceSignal(
                type="bearish_hidden",
                indicator=indicator_name,
                bar_index_1=idx1,
                bar_index_2=idx2,
                price_1=float(close.iloc[idx1]),
                price_2=float(close.iloc[idx2]),
                indicator_1=float(indicator_series.iloc[ih1]),
                indicator_2=float(indicator_series.iloc[ih2]),
                strength=_calc_strength(
                    close.iloc[idx1], close.iloc[idx2],
                    indicator_series.iloc[ih1], indicator_series.iloc[ih2],
                ),
                timestamp=_ts(idx2),
            ))
            break

    return signals


def _nearest(indices: list[int], target: int, tolerance: int) -> Optional[int]:
    """Return the index from *indices* closest to *target* within *tolerance*."""
    best: Optional[int] = None
    best_dist = tolerance + 1
    for idx in indices:
        dist = abs(idx - target)
        if dist < best_dist:
            best_dist = dist
            best = idx
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_rsi_divergence(
    df: pd.DataFrame,
    rsi_series: pd.Series,
    lookback: int = 5,
    min_bars_apart: int = 5,
    max_bars_apart: int = 50,
) -> list[DivergenceSignal]:
    """Detect all RSI divergence types (regular + hidden, bullish + bearish).

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame (must contain a ``close`` column).
    rsi_series : pd.Series
        Pre-computed RSI values aligned with *df*.
    lookback : int
        Window for identifying swing highs/lows.
    min_bars_apart, max_bars_apart : int
        Constraints on distance between the two swing points.

    Returns
    -------
    list[DivergenceSignal]
    """
    return _detect_divergences(
        df, rsi_series, "rsi", lookback, min_bars_apart, max_bars_apart
    )


def detect_macd_divergence(
    df: pd.DataFrame,
    macd_hist: pd.Series,
    lookback: int = 5,
    min_bars_apart: int = 5,
    max_bars_apart: int = 50,
) -> list[DivergenceSignal]:
    """Detect all MACD-histogram divergence types.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame.
    macd_hist : pd.Series
        MACD histogram (MACD line − signal line) aligned with *df*.
    lookback : int
        Window for identifying swing highs/lows.
    min_bars_apart, max_bars_apart : int
        Constraints on distance between the two swing points.

    Returns
    -------
    list[DivergenceSignal]
    """
    return _detect_divergences(
        df, macd_hist, "macd", lookback, min_bars_apart, max_bars_apart
    )


def detect_all_divergences(
    df: pd.DataFrame,
    indicators: dict,
) -> list[DivergenceSignal]:
    """Convenience wrapper – run both RSI and MACD divergence detection.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame.
    indicators : dict
        Must contain ``'rsi'`` (pd.Series) and ``'macd_hist'`` (pd.Series).

    Returns
    -------
    list[DivergenceSignal]
        Combined list sorted by bar_index_2 (most recent swing).
    """
    results: list[DivergenceSignal] = []
    if "rsi" in indicators and indicators["rsi"] is not None:
        results.extend(detect_rsi_divergence(df, indicators["rsi"]))
    if "macd_hist" in indicators and indicators["macd_hist"] is not None:
        results.extend(detect_macd_divergence(df, indicators["macd_hist"]))
    # Sort by second swing index so newest divergences come last
    results.sort(key=lambda d: d.bar_index_2)
    return results


def divergences_to_dict(divs: list[DivergenceSignal]) -> list[dict]:
    """Serialise a list of DivergenceSignal to plain dicts."""
    return [asdict(d) for d in divs]


def recent_divergences(
    divs: list[DivergenceSignal], n: int = 3
) -> list[DivergenceSignal]:
    """Return the *n* most recent divergences (by bar_index_2)."""
    sorted_divs = sorted(divs, key=lambda d: d.bar_index_2, reverse=True)
    return sorted_divs[:n]
