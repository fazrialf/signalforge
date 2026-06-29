"""SignalForge — Analysis Pipeline
Combines Sprint 2 (SMC) and Sprint 3 (Patterns) detectors into a single analysis pass.
Run on every candle close to get the current structural + pattern picture."""
import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from core.swing_points import detect_swing_points, swing_points_to_dict
from core.market_structure import analyse_structure, MarketStructure, Bias
from core.smc import (
    detect_sr_levels, SRLevel,
    detect_fvgs, FairValueGap, get_active_fvgs,
    detect_order_blocks, OrderBlock, get_active_order_blocks,
    detect_liquidity_pools, LiquidityPool, recent_liquidity_grab,
    calc_premium_discount, PremiumDiscountZone,
    nearest_sr,
)
from core.candlestick import detect_all_patterns as detect_candlestick_patterns, CandlestickPattern, patterns_to_dict, recent_patterns
from core.divergence import detect_all_divergences, DivergenceSignal, divergences_to_dict
from core.squeeze import calc_squeeze, get_current_squeeze, is_squeeze_firing, SqueezeState, squeeze_to_dict
from core.volume_analysis import detect_all_volume_signals, VolumeSignal, volume_signals_to_dict
from core.chart_patterns import detect_all_chart_patterns, ChartPattern, chart_patterns_to_dict

logger = logging.getLogger(__name__)


@dataclass
class SMCAnalysisResult:
    """Complete SMC + Pattern analysis snapshot for one timeframe."""
    timeframe: str
    current_price: float
    # Sprint 2 — SMC
    swing_points: list  # list[dict] JSON-safe
    structure: Optional[MarketStructure]
    sr_levels: list[SRLevel]
    fvgs: list[FairValueGap]
    active_fvgs: list[FairValueGap]
    order_blocks: list[OrderBlock]
    active_obs: list[OrderBlock]
    liquidity_pools: list[LiquidityPool]
    recent_grab: Optional[LiquidityPool]
    premium_discount: Optional[PremiumDiscountZone]
    nearest_resistance: Optional[SRLevel]
    nearest_support: Optional[SRLevel]
    # Sprint 3 — Pattern Detection
    candlestick_patterns: list[CandlestickPattern] = field(default_factory=list)
    chart_patterns: list[ChartPattern] = field(default_factory=list)
    divergences: list[DivergenceSignal] = field(default_factory=list)
    volume_signals: list[VolumeSignal] = field(default_factory=list)
    squeeze: Optional[SqueezeState] = None
    squeeze_firing: bool = False

    def to_dict(self) -> dict:
        """Serialise to dict for LLM prompt context or logging."""
        pd_summary = None
        if self.premium_discount:
            pd_summary = {
                "zone": self.premium_discount.zone,
                "equilibrium": self.premium_discount.equilibrium,
                "premium_top": self.premium_discount.premium_top,
                "discount_bot": self.premium_discount.discount_bot,
            }

        return {
            "timeframe": self.timeframe,
            "current_price": self.current_price,
            "bias": self.structure.bias.value if self.structure else "unknown",
            "structure": self.structure.to_dict() if self.structure else {},
            "sr_levels": [
                {"price": l.price, "type": l.type, "touches": l.touches, "strength": l.strength}
                for l in self.sr_levels[:10]
            ],
            "active_fvgs": [
                {"top": f.top, "bottom": f.bottom, "direction": f.direction,
                 "fill_pct": round(f.fill_pct, 1), "midpoint": round(f.midpoint, 2)}
                for f in self.active_fvgs
            ],
            "active_order_blocks": [
                {"top": ob.top, "bottom": ob.bottom, "direction": ob.direction}
                for ob in self.active_obs
            ],
            "recent_liquidity_grab": {
                "price": self.recent_grab.price,
                "type": self.recent_grab.type,
                "swept_at": str(self.recent_grab.swept_at),
            } if self.recent_grab else None,
            "premium_discount": pd_summary,
            "nearest_resistance": round(self.nearest_resistance.price, 2) if self.nearest_resistance else None,
            "nearest_support": round(self.nearest_support.price, 2) if self.nearest_support else None,
            # Sprint 3
            "candlestick_patterns": patterns_to_dict(self.candlestick_patterns[:5]),
            "chart_patterns": chart_patterns_to_dict(self.chart_patterns[:3]),
            "divergences": divergences_to_dict(self.divergences[:3]),
            "volume_signals": volume_signals_to_dict(self.volume_signals[:5]),
            "squeeze": squeeze_to_dict(self.squeeze) if self.squeeze else None,
            "squeeze_firing": self.squeeze_firing,
        }

    def to_summary(self) -> str:
        """Human-readable summary for LLM prompts."""
        lines = [f"=== {self.timeframe} SMC Analysis ==="]
        if self.structure:
            lines.append(self.structure.to_summary())

        lines.append(f"\nS/R Levels: {len(self.sr_levels)} detected")
        if self.nearest_support:
            lines.append(f"  Support above: ${self.nearest_support.price:,.2f} ({self.nearest_support.touches} touches)")
        if self.nearest_resistance:
            lines.append(f"  Resistance below: ${self.nearest_resistance.price:,.2f} ({self.nearest_resistance.touches} touches)")

        if self.active_fvgs:
            lines.append(f"\nActive FVGs: {len(self.active_fvgs)}")
            for fvg in self.active_fvgs[:3]:
                lines.append(f"  {fvg.direction.upper()} FVG: ${fvg.bottom:,.2f}–${fvg.top:,.2f} "
                             f"(filled: {round(fvg.fill_pct, 1)}%)")

        if self.active_obs:
            lines.append(f"\nActive Order Blocks: {len(self.active_obs)}")
            for ob in self.active_obs[:3]:
                lines.append(f"  {ob.direction.upper()} OB: ${ob.bottom:,.2f}–${ob.top:,.2f}")

        if self.recent_grab:
            lines.append(f"\nRecent Liquidity Grab: {self.recent_grab.type} @ ${self.recent_grab.price:,.2f}")

        if self.premium_discount:
            lines.append(f"\nZone: {self.premium_discount.zone.upper()} "
                         f"(EQ @ ${self.premium_discount.equilibrium:,.2f})")

        # Sprint 3 — Patterns
        if self.candlestick_patterns:
            recent = recent_patterns(self.candlestick_patterns, n=3)
            lines.append(f"\nCandlestick Patterns: {len(self.candlestick_patterns)} detected")
            for p in recent:
                lines.append(f"  {p.direction.upper()} {p.pattern} (strength={round(p.strength, 2)})")

        if self.chart_patterns:
            lines.append(f"\nChart Patterns: {len(self.chart_patterns)} detected")
            for cp in self.chart_patterns[:2]:
                lines.append(f"  {cp.direction.upper()} {cp.pattern} — key level ${cp.key_level:,.2f}")

        if self.divergences:
            lines.append(f"\nDivergences: {len(self.divergences)} detected")
            for d in self.divergences[:2]:
                lines.append(f"  {d.type} ({d.indicator.upper()}) strength={round(d.strength, 2)}")

        if self.volume_signals:
            lines.append(f"\nVolume Signals: {len(self.volume_signals)} detected")
            for v in self.volume_signals[:2]:
                lines.append(f"  {v.direction.upper()} {v.type} (RVOL={round(v.rvol, 1)}x)")

        if self.squeeze:
            sq_status = "💥 FIRING" if self.squeeze_firing else ("in squeeze" if self.squeeze.in_squeeze else "no squeeze")
            lines.append(f"\nSqueeze: {sq_status} | momentum={round(self.squeeze.momentum, 4)} ({self.squeeze.momentum_direction})")

        return "\n".join(lines)


def analyse_timeframe(
    df: pd.DataFrame,
    timeframe: str = "1h",
    lookback: int = 5,
    current_price: Optional[float] = None,
) -> SMCAnalysisResult:
    """
    Run the full SMC analysis pipeline on a single timeframe's OHLCV data.

    Args:
        df: OHLCV DataFrame with columns: open, high, low, close, volume
        timeframe: label (e.g. '1h', '4h', '1d')
        lookback: N-bar lookback for swing point detection
        current_price: override current price (defaults to last close)

    Returns:
        SMCAnalysisResult with all detected features
    """
    price = current_price if current_price is not None else df["close"].iloc[-1]
    price = float(price)

    # 1. Swing points
    swings = detect_swing_points(df, lookback=lookback)
    swings_dict = swing_points_to_dict(swings)

    # 2. Market structure
    structure = analyse_structure(df, lookback=lookback)

    # 3. S/R levels
    sr_levels = detect_sr_levels(df, swings)
    nearest_sup, nearest_res = nearest_sr(sr_levels, price)

    # 4. FVGs
    fvgs = detect_fvgs(df)
    active_fvgs = get_active_fvgs(fvgs, price)

    # 5. Order Blocks
    obs = detect_order_blocks(df, swings)
    active_obs = get_active_order_blocks(obs, price)

    # 6. Liquidity pools
    pools = detect_liquidity_pools(df, swings)
    recent_grab = recent_liquidity_grab(pools, df=df)

    # 7. Premium / Discount
    pd_zone = None
    # Use last swing high/low for range calc
    swing_highs = [s for s in swings if s.type == "high"]
    swing_lows = [s for s in swings if s.type == "low"]
    if swing_highs and swing_lows:
        top = max(s.price for s in swing_highs[-3:])
        bot = min(s.price for s in swing_lows[-3:])
        if top > bot:
            pd_zone = calc_premium_discount(top, bot, price)

    # --- Sprint 3: Pattern Detection ---

    # 8. Candlestick patterns
    candle_patterns = detect_candlestick_patterns(df)

    # 9. Chart patterns (requires swing points)
    c_patterns = detect_all_chart_patterns(df, swings)

    # 10. Divergences (requires indicator data — use RSI/MACD if available in df)
    divs: list[DivergenceSignal] = []
    try:
        from core.indicators import add_indicators
        df_with_ind = add_indicators(df.copy())
        indicators = {
            "rsi": df_with_ind["rsi"] if "rsi" in df_with_ind.columns else None,
            "macd_hist": df_with_ind["macd_hist"] if "macd_hist" in df_with_ind.columns else None,
        }
        # Filter out None values
        indicators = {k: v for k, v in indicators.items() if v is not None}
        if indicators:
            divs = detect_all_divergences(df, indicators)
    except Exception:
        pass  # Indicators may not be available in test context

    # 11. Volume signals
    vol_signals = detect_all_volume_signals(df)

    # 12. Squeeze
    squeeze_states = calc_squeeze(df)
    current_squeeze = squeeze_states[-1] if squeeze_states else None
    squeeze_firing = is_squeeze_firing(df)

    return SMCAnalysisResult(
        timeframe=timeframe,
        current_price=price,
        swing_points=swings_dict,
        structure=structure,
        sr_levels=sr_levels,
        fvgs=fvgs,
        active_fvgs=active_fvgs,
        order_blocks=obs,
        active_obs=active_obs,
        liquidity_pools=pools,
        recent_grab=recent_grab,
        premium_discount=pd_zone,
        nearest_resistance=nearest_res,
        nearest_support=nearest_sup,
        # Sprint 3
        candlestick_patterns=candle_patterns,
        chart_patterns=c_patterns,
        divergences=divs,
        volume_signals=vol_signals,
        squeeze=current_squeeze,
        squeeze_firing=squeeze_firing,
    )


def analyse_all_timeframes(
    data: dict[str, pd.DataFrame],
    current_price: Optional[float] = None,
) -> dict[str, SMCAnalysisResult]:
    """
    Run SMC analysis on all available timeframes.

    Args:
        data: dict of {timeframe: DataFrame} — as returned by DataFetcher.load_all()
        current_price: optional price override

    Returns:
        dict of {timeframe: SMCAnalysisResult}
    """
    results = {}
    for tf, df in data.items():
        # Determine lookback based on timeframe (higher TF = more bars needed)
        if tf in ("1d", "1w", "1M"):
            lb = 7
        elif tf in ("4h", "6h", "12h"):
            lb = 5
        elif tf in ("1h", "2h"):
            lb = 5
        elif tf in ("15m", "30m"):
            lb = 4
        else:
            lb = 3  # 1m, 5m

        if len(df) < lb * 3:
            logger.warning("Not enough data for %s to run SMC analysis (%d bars, need %d)",
                           tf, len(df), lb * 3)
            continue

        results[tf] = analyse_timeframe(df, tf, lookback=lb, current_price=current_price)

    return results


if __name__ == "__main__":
    import asyncio
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from db.schema import init_db
    from data.fetcher import DataFetcher

    async def test():
        init_db()
        fetcher = DataFetcher("BTC/USDT", ["1h", "4h", "1d"], history_bars=200)
        data = await fetcher.load_all()
        await fetcher.close()

        results = analyse_all_timeframes(data)
        for tf, r in results.items():
            print(f"\n{'='*60}")
            print(r.to_summary())
            print(f"{'='*60}")

    asyncio.run(test())
