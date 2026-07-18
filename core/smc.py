"""
SignalForge — Support & Resistance + Fair Value Gap + Order Block + Liquidity Pools
All SMC detection in one module for efficient computation.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from core.swing_points import SwingPoint, detect_swing_points

logger = logging.getLogger(__name__)


# ================================================================
# SUPPORT & RESISTANCE
# ================================================================

@dataclass
class SRLevel:
    price: float
    type: str          # 'resistance' or 'support'
    touches: int       # how many times price respected this level
    strength: float    # 0–1 score
    last_touch: Optional[pd.Timestamp] = None


def detect_sr_levels(df: pd.DataFrame,
                     swing_points: list[SwingPoint],
                     cluster_pct: float = 0.003) -> list[SRLevel]:
    """
    Cluster swing highs into resistance levels and swing lows into support levels.
    Levels within cluster_pct (0.3%) of each other are merged.
    """
    levels: list[SRLevel] = []

    for sr_type, point_type in [("resistance", "high"), ("support", "low")]:
        prices = sorted(
            [(p.price, p.timestamp) for p in swing_points if p.type == point_type],
            key=lambda x: x[0]
        )
        if not prices:
            continue

        clusters: list[list] = []
        current_cluster = [prices[0]]

        for price, ts in prices[1:]:
            ref = current_cluster[0][0]
            if abs(price - ref) / ref <= cluster_pct:
                current_cluster.append((price, ts))
            else:
                clusters.append(current_cluster)
                current_cluster = [(price, ts)]
        clusters.append(current_cluster)

        for cluster in clusters:
            avg_price = sum(p[0] for p in cluster) / len(cluster)
            last_ts   = max(p[1] for p in cluster)
            strength  = min(1.0, len(cluster) / 5)  # 5+ touches = max strength
            levels.append(SRLevel(
                price=round(avg_price, 2),
                type=sr_type,
                touches=len(cluster),
                strength=strength,
                last_touch=last_ts
            ))

    levels.sort(key=lambda x: x.price)
    return levels


def nearest_sr(levels: list[SRLevel], price: float,
               max_dist_pct: float = 0.02) -> tuple[Optional[SRLevel], Optional[SRLevel]]:
    """
    Return (nearest_support_below, nearest_resistance_above) within max_dist_pct.
    """
    support    = None
    resistance = None
    for lvl in levels:
        dist = abs(lvl.price - price) / price
        if dist > max_dist_pct:
            continue
        if lvl.type == "support" and lvl.price < price:
            if support is None or lvl.price > support.price:
                support = lvl
        elif lvl.type == "resistance" and lvl.price > price:
            if resistance is None or lvl.price < resistance.price:
                resistance = lvl
    return support, resistance


# ================================================================
# FAIR VALUE GAP (FVG)
# ================================================================

@dataclass
class FairValueGap:
    top: float
    bottom: float
    direction: str       # 'bullish' or 'bearish'
    formed_at: pd.Timestamp
    bar_index: int
    filled: bool = False
    fill_pct: float = 0.0  # 0–100% how much has been filled

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size_pct(self) -> float:
        return (self.top - self.bottom) / self.bottom * 100


def detect_fvgs(df: pd.DataFrame,
                min_size_pct: float = 0.1) -> list[FairValueGap]:
    """
    Detect Fair Value Gaps (3-candle pattern).

    Bullish FVG: candle[i-2].high < candle[i].low
      (gap between candle 1 high and candle 3 low — price moved up too fast)

    Bearish FVG: candle[i-2].low > candle[i].high
      (gap between candle 1 low and candle 3 high — price moved down too fast)
    """
    fvgs: list[FairValueGap] = []
    n = len(df)

    for i in range(2, n):
        c1_high = df["high"].iloc[i - 2]
        c1_low  = df["low"].iloc[i - 2]
        c3_high = df["high"].iloc[i]
        c3_low  = df["low"].iloc[i]
        ts      = df.index[i]

        # Bullish FVG: c1 high is below c3 low
        if c1_high < c3_low:
            size_pct = (c3_low - c1_high) / c1_high * 100
            if size_pct >= min_size_pct:
                fvgs.append(FairValueGap(
                    top=c3_low, bottom=c1_high,
                    direction="bullish",
                    formed_at=ts, bar_index=i
                ))

        # Bearish FVG: c1 low is above c3 high
        elif c1_low > c3_high:
            size_pct = (c1_low - c3_high) / c3_high * 100
            if size_pct >= min_size_pct:
                fvgs.append(FairValueGap(
                    top=c1_low, bottom=c3_high,
                    direction="bearish",
                    formed_at=ts, bar_index=i
                ))

    # Mark filled FVGs: price returned into the gap zone
    closes = df["close"].values
    for fvg in fvgs:
        post_bars = closes[fvg.bar_index + 1:]
        if len(post_bars) == 0:
            continue
        if fvg.direction == "bullish":
            # Filled if price came back down into the gap
            min_post = np.min(post_bars)
            if min_post <= fvg.bottom:
                fvg.filled = True
                fvg.fill_pct = 100.0
            elif min_post < fvg.top:
                fvg.fill_pct = (fvg.top - min_post) / (fvg.top - fvg.bottom) * 100
        else:
            # Bearish FVG: filled if price came back up into the gap
            max_post = np.max(post_bars)
            if max_post >= fvg.top:
                fvg.filled = True
                fvg.fill_pct = 100.0
            elif max_post > fvg.bottom:
                fvg.fill_pct = (max_post - fvg.bottom) / (fvg.top - fvg.bottom) * 100

    return fvgs


def get_active_fvgs(fvgs: list[FairValueGap],
                    current_price: float,
                    max_dist_pct: float = 0.05) -> list[FairValueGap]:
    """Return unfilled FVGs near the current price."""
    result = []
    for fvg in fvgs:
        if fvg.filled:
            continue
        dist = abs(fvg.midpoint - current_price) / current_price
        if dist <= max_dist_pct:
            result.append(fvg)
    return sorted(result, key=lambda f: abs(f.midpoint - current_price))


# ================================================================
# ORDER BLOCK
# ================================================================

@dataclass
class OrderBlock:
    top: float
    bottom: float
    direction: str       # 'bullish' or 'bearish'
    formed_at: pd.Timestamp
    bar_index: int
    broken: bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


def detect_order_blocks(df: pd.DataFrame,
                         swing_points: list[SwingPoint]) -> list[OrderBlock]:
    """
    Order Block: the last bearish candle before a bullish impulse move (bullish OB),
    or the last bullish candle before a bearish impulse move (bearish OB).

    Simplified detection:
    - Bullish OB: candle is bearish (close < open) AND the next 3 bars
      all close higher AND combined range > 2x the OB candle range.
    - Bearish OB: candle is bullish (close > open) AND the next 3 bars
      all close lower AND combined range > 2x the OB candle range.
    """
    obs: list[OrderBlock] = []
    n = len(df)
    opens  = df["open"].values
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values

    for i in range(1, n - 3):
        candle_range = abs(closes[i] - opens[i])
        if candle_range == 0:
            continue

        # Impulse size: range of next 3 bars
        impulse_high = np.max(highs[i + 1:i + 4])
        impulse_low  = np.min(lows[i + 1:i + 4])
        impulse_size = impulse_high - impulse_low

        # Bullish OB: bearish candle before bullish impulse
        if (closes[i] < opens[i] and               # bearish candle
                all(closes[i + k] > closes[i] for k in range(1, 4)) and  # 3 up closes
                impulse_size > 1.5 * candle_range): # impulse > 1.5x OB size
            obs.append(OrderBlock(
                top=opens[i], bottom=lows[i],
                direction="bullish",
                formed_at=df.index[i], bar_index=i
            ))

        # Bearish OB: bullish candle before bearish impulse
        elif (closes[i] > opens[i] and             # bullish candle
                all(closes[i + k] < closes[i] for k in range(1, 4)) and  # 3 down closes
                impulse_size > 1.5 * candle_range):
            obs.append(OrderBlock(
                top=highs[i], bottom=closes[i],
                direction="bearish",
                formed_at=df.index[i], bar_index=i
            ))

    # Mark broken OBs: price has closed through them
    for ob in obs:
        post_closes = closes[ob.bar_index + 4:]
        if len(post_closes) == 0:
            continue
        if ob.direction == "bullish" and np.any(post_closes < ob.bottom):
            ob.broken = True
        elif ob.direction == "bearish" and np.any(post_closes > ob.top):
            ob.broken = True

    return obs


def get_active_order_blocks(obs: list[OrderBlock],
                             current_price: float,
                             max_dist_pct: float = 0.05) -> list[OrderBlock]:
    """Return unbroken OBs near the current price."""
    return [
        ob for ob in obs
        if not ob.broken and
        abs(ob.midpoint - current_price) / current_price <= max_dist_pct
    ]


# ================================================================
# LIQUIDITY POOLS
# ================================================================

@dataclass
class LiquidityPool:
    price: float
    type: str            # 'buy_side' (above swing highs) or 'sell_side' (below swing lows)
    formed_at: pd.Timestamp
    swept: bool = False
    swept_at: Optional[pd.Timestamp] = None


def detect_liquidity_pools(df: pd.DataFrame,
                            swing_points: list[SwingPoint]) -> list[LiquidityPool]:
    """
    Liquidity pools sit just above swing highs (buy-side) and just below swing lows (sell-side).
    When price sweeps through and reverses, it’s a liquidity grab.
    """
    pools: list[LiquidityPool] = []
    highs  = df["high"].values
    lows   = df["low"].values

    for p in swing_points:
        if p.type == "high":
            pools.append(LiquidityPool(
                price=p.price,
                type="buy_side",
                formed_at=p.timestamp
            ))
        else:
            pools.append(LiquidityPool(
                price=p.price,
                type="sell_side",
                formed_at=p.timestamp
            ))

    # Mark swept pools: did a later bar wick through and reverse?
    for pool in pools:
        try:
            idx_loc = df.index.get_indexer([pool.formed_at], method="nearest")[0]
        except (TypeError, ValueError, KeyError):
            idx_loc = df.index.get_loc(pool.formed_at) if pool.formed_at in df.index else -1
        if idx_loc < 0 or idx_loc + 1 >= len(df):
            continue
        post_df = df.iloc[idx_loc + 1:]
        if pool.type == "buy_side":
            # Swept if any bar wicked above the level then closed below it
            for idx, row in post_df.iterrows():
                if row["high"] > pool.price and row["close"] < pool.price:
                    pool.swept = True
                    pool.swept_at = idx
                    break
        else:
            for idx, row in post_df.iterrows():
                if row["low"] < pool.price and row["close"] > pool.price:
                    pool.swept = True
                    pool.swept_at = idx
                    break

    return pools


def recent_liquidity_grab(pools: list[LiquidityPool],
                           lookback_bars: int = 5,
                           df: Optional[pd.DataFrame] = None) -> Optional[LiquidityPool]:
    """Return the most recently swept pool (potential liquidity grab)."""
    swept = [p for p in pools if p.swept and p.swept_at is not None]
    if not swept:
        return None
    swept.sort(key=lambda p: p.swept_at, reverse=True)
    return swept[0]


# ================================================================
# PREMIUM / DISCOUNT ZONES
# ================================================================

@dataclass
class PremiumDiscountZone:
    equilibrium: float    # 50% of the range
    premium_top: float    # top of the range (swing high)
    premium_start: float  # 75% level
    discount_end: float   # 25% level
    discount_bot: float   # bottom of the range (swing low)
    current_price: float
    zone: str             # 'premium', 'discount', or 'equilibrium'


def calc_premium_discount(swing_high: float,
                           swing_low: float,
                           current_price: float) -> PremiumDiscountZone:
    """
    Calculate premium/discount zone from the most recent swing high/low range.
    - Premium zone: price above 50% (expensive — look for shorts)
    - Discount zone: price below 50% (cheap — look for longs)
    - Equilibrium: within 10% of the 50% level
    """
    rng          = swing_high - swing_low
    eq           = swing_low + rng * 0.5
    prem_start   = swing_low + rng * 0.75
    disc_end     = swing_low + rng * 0.25
    eq_band      = rng * 0.10

    if abs(current_price - eq) <= eq_band:
        zone = "equilibrium"
    elif current_price > eq:
        zone = "premium"
    else:
        zone = "discount"

    return PremiumDiscountZone(
        equilibrium=round(eq, 2),
        premium_top=round(swing_high, 2),
        premium_start=round(prem_start, 2),
        discount_end=round(disc_end, 2),
        discount_bot=round(swing_low, 2),
        current_price=round(current_price, 2),
        zone=zone
    )


# ================================================================
# EQUAL HIGHS / EQUAL LOWS (Liquidity Sweep Target Detection)
# ================================================================

@dataclass
class EqualLevel:
    """A cluster of swing points at nearly the same price — resting liquidity.

    Attributes:
        price: Average price of the cluster.
        type: 'equal_highs' or 'equal_lows'.
        touches: Number of swing points forming this level.
        first_bar: Bar index of the first touch.
        last_bar: Bar index of the most recent touch.
        swept: Whether price has already broken beyond this level.
        sweep_bar: Bar index where the sweep occurred (None if not swept).
        reclaimed: Whether price closed back inside after sweep.
    """
    price: float
    type: str           # 'equal_highs' or 'equal_lows'
    touches: int
    first_bar: int
    last_bar: int
    swept: bool = False
    sweep_bar: Optional[int] = None
    reclaimed: bool = False


def detect_equal_levels(
    df: pd.DataFrame,
    swings: list,
    tolerance_pct: float = 0.001,
    min_touches: int = 3,
    lookback_bars: int = 100,
) -> list[EqualLevel]:
    """Detect equal highs and equal lows from swing points.

    Equal highs/lows are clusters of swing points at nearly the same price,
    indicating resting liquidity (buy stops above equal highs, sell stops
    below equal lows). These are prime sweep targets for institutional
    order flow.

    Args:
        df: OHLCV DataFrame.
        swings: List of SwingPoint objects from detect_swing_points().
        tolerance_pct: Max % difference between swing prices to be considered
            'equal'. Default 0.1% — tight enough for scalping.
        min_touches: Minimum number of swings at the same level to qualify.
            Default 3 (classic ICT equal highs/lows).
        lookback_bars: Only consider swings within this many bars from end.

    Returns:
        List of EqualLevel objects, sorted by proximity to current price.
    """
    if not swings or df is None or len(df) < 10:
        return []

    current_price = float(df["close"].iloc[-1])
    last_bar_idx = len(df) - 1
    cutoff_bar = max(0, last_bar_idx - lookback_bars)

    # Separate highs and lows within lookback window.
    # SwingPoint uses `.index` (not bar_index) — fall back to bar_index for
    # any alternate swing types that may expose that attribute.
    def _swing_bar(s) -> int:
        return int(getattr(s, "index", getattr(s, "bar_index", 0)) or 0)

    recent_highs = [
        s for s in swings
        if s.type == "high" and _swing_bar(s) >= cutoff_bar
    ]
    recent_lows = [
        s for s in swings
        if s.type == "low" and _swing_bar(s) >= cutoff_bar
    ]

    results: list[EqualLevel] = []

    # Cluster swing highs
    _cluster_swings(recent_highs, "equal_highs", tolerance_pct, min_touches,
                    df, current_price, results)

    # Cluster swing lows
    _cluster_swings(recent_lows, "equal_lows", tolerance_pct, min_touches,
                    df, current_price, results)

    # Sort by distance to current price (closest first)
    results.sort(key=lambda lvl: abs(lvl.price - current_price))

    return results


def _cluster_swings(
    swings: list,
    level_type: str,
    tolerance_pct: float,
    min_touches: int,
    df: pd.DataFrame,
    current_price: float,
    results: list[EqualLevel],
) -> None:
    """Group swing points into price clusters and detect equal levels."""
    if len(swings) < min_touches:
        return

    # Sort by price for clustering
    sorted_swings = sorted(swings, key=lambda s: s.price)
    used = set()

    for i, anchor in enumerate(sorted_swings):
        if i in used:
            continue

        cluster = [anchor]
        used.add(i)

        # Find all swings within tolerance of this anchor
        for j in range(i + 1, len(sorted_swings)):
            if j in used:
                continue
            candidate = sorted_swings[j]
            pct_diff = abs(candidate.price - anchor.price) / anchor.price
            if pct_diff <= tolerance_pct:
                cluster.append(candidate)
                used.add(j)
            else:
                break  # sorted, so all subsequent are further away

        if len(cluster) >= min_touches:
            avg_price = sum(s.price for s in cluster) / len(cluster)
            # Prefer SwingPoint.index; fall back to bar_index / 0
            bar_indices = [
                int(getattr(s, "index", getattr(s, "bar_index", 0)) or 0)
                for s in cluster
            ]
            first_bar = min(bar_indices)
            last_bar = max(bar_indices)

            # Check if level has been swept (price went beyond it)
            swept, sweep_bar, reclaimed = _check_sweep(
                df, avg_price, level_type, last_bar
            )

            results.append(EqualLevel(
                price=round(avg_price, 6),
                type=level_type,
                touches=len(cluster),
                first_bar=first_bar,
                last_bar=last_bar,
                swept=swept,
                sweep_bar=sweep_bar,
                reclaimed=reclaimed,
            ))


def _check_sweep(
    df: pd.DataFrame,
    level_price: float,
    level_type: str,
    formed_bar: int,
) -> tuple[bool, Optional[int], bool]:
    """Check if an equal level has been swept and/or reclaimed.

    Args:
        df: OHLCV DataFrame.
        level_price: The equal level price.
        level_type: 'equal_highs' or 'equal_lows'.
        formed_bar: Bar index when the level was fully formed (last touch).

    Returns:
        (swept, sweep_bar, reclaimed)
    """
    if formed_bar >= len(df) - 1:
        return False, None, False

    # Check bars after formation
    post_bars = df.iloc[formed_bar + 1:]
    if post_bars.empty:
        return False, None, False

    high = post_bars["high"].astype(float).values
    low = post_bars["low"].astype(float).values
    close = post_bars["close"].astype(float).values

    swept = False
    sweep_bar = None
    reclaimed = False

    if level_type == "equal_highs":
        # Sweep = price went above the level (took buy stops)
        for idx in range(len(high)):
            if high[idx] > level_price:
                swept = True
                sweep_bar = formed_bar + 1 + idx
                # Reclaim = price closed back below after sweep
                if idx < len(close) - 1:
                    # Check if any subsequent close is below the level
                    subsequent_closes = close[idx + 1:]
                    if len(subsequent_closes) > 0 and subsequent_closes[-1] < level_price:
                        reclaimed = True
                break

    elif level_type == "equal_lows":
        # Sweep = price went below the level (took sell stops)
        for idx in range(len(low)):
            if low[idx] < level_price:
                swept = True
                sweep_bar = formed_bar + 1 + idx
                # Reclaim = price closed back above after sweep
                if idx < len(close) - 1:
                    subsequent_closes = close[idx + 1:]
                    if len(subsequent_closes) > 0 and subsequent_closes[-1] > level_price:
                        reclaimed = True
                break

    return swept, sweep_bar, reclaimed


def get_unsweep_levels(
    equal_levels: list[EqualLevel],
) -> list[EqualLevel]:
    """Return only unswept equal levels — active liquidity targets."""
    return [lvl for lvl in equal_levels if not lvl.swept]


def get_swept_reclaimed_levels(
    equal_levels: list[EqualLevel],
) -> list[EqualLevel]:
    """Return levels that were swept AND reclaimed — entry signals."""
    return [lvl for lvl in equal_levels if lvl.swept and lvl.reclaimed]

