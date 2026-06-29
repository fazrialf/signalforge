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
