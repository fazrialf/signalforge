"""SignalForge — Tier-Weighted Confluence Scoring Engine
======================================================
Scores how many independent signals align for a potential trade direction.

Each signal detected in an :class:`SMCAnalysisResult` is classified into one
of three tiers with a corresponding weight:

* **Tier 1 — Structure (3×)**: BOS/ChOS, market structure bias, nearby S/R,
  active order block near price, MTF bias alignment.
* **Tier 2 — Trigger (2×)**: FVG entry, liquidity grab+reversal, candlestick
  reversal, squeeze firing, volume surge/climax, chart pattern breakout.
* **Tier 3 — Confirmation (1×)**: Premium/discount zone, divergence,
  RSI/MACD alignment, volume trend (OBV).

The main entry point is :func:`score_confluence` which returns a
:class:`ConfluenceScore` summarising the overall directional conviction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from signals.pipeline import SMCAnalysisResult
from signals.mtf_bias import MTFBias
from core.market_structure import Bias, StructureEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier weight constants
# ---------------------------------------------------------------------------
TIER_WEIGHTS: dict[int, int] = {1: 3, 2: 2, 3: 1}

# Proximity threshold: S/R or OB must be within this % of current price
SR_PROXIMITY_PCT = 0.005   # 0.5 %
OB_PROXIMITY_PCT = 0.01    # 1.0 %
FVG_PROXIMITY_PCT = 0.015  # 1.5 %

# Default threshold for ``meets_threshold`` flag — kept for direct callers,
# but main.py always passes MIN_CONFLUENCE_SCORE=6 from settings explicitly.
DEFAULT_THRESHOLD = 6


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ConfluenceFactor:
    """A single signal contributing to the confluence score.

    Attributes
    ----------
    name : str
        Machine-readable identifier, e.g. ``'bos_bullish'``.
    tier : int
        1 = Structure, 2 = Trigger, 3 = Confirmation.
    direction : str
        ``'bullish'`` or ``'bearish'``.
    weight : int
        Tier weight (3, 2, or 1).
    description : str
        Short human-readable reason for this factor.
    """

    name: str
    tier: int
    direction: str
    weight: int
    description: str


@dataclass
class ConfluenceScore:
    """Aggregated confluence result for a single analysis snapshot.

    Attributes
    ----------
    direction : str
        ``'bullish'``, ``'bearish'``, or ``'neutral'``.
    raw_score : int
        Sum of **all** factor weights (both directions combined).
    bullish_score : int
        Sum of bullish-only factor weights.
    bearish_score : int
        Sum of bearish-only factor weights.
    net_score : int
        ``bullish_score - bearish_score`` (positive → bullish bias).
    factors : list[ConfluenceFactor]
        Every contributing factor.
    meets_threshold : bool
        ``abs(net_score) >= threshold`` (default 8).
    dominant_direction : str
        The direction with the higher absolute score.
    """

    direction: str
    raw_score: int
    bullish_score: int
    bearish_score: int
    net_score: int
    factors: list[ConfluenceFactor] = field(default_factory=list)
    meets_threshold: bool = False
    dominant_direction: str = "neutral"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add(factors: list[ConfluenceFactor],
         name: str,
         tier: int,
         direction: str,
         description: str) -> None:
    """Append a :class:`ConfluenceFactor` to *factors* with the correct weight."""
    weight = TIER_WEIGHTS.get(tier, 1)
    factors.append(ConfluenceFactor(
        name=name,
        tier=tier,
        direction=direction,
        weight=weight,
        description=description,
    ))


def _near(level_price: float, current_price: float, pct: float) -> bool:
    """Return ``True`` if *level_price* is within *pct* of *current_price*."""
    if current_price == 0:
        return False
    return abs(level_price - current_price) / abs(current_price) <= pct


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_confluence(
    result: SMCAnalysisResult,
    mtf_aligned: bool = False,
    threshold: int = DEFAULT_THRESHOLD,
    mtf_bias: Optional[MTFBias] = None,
) -> ConfluenceScore:
    """Evaluate confluence factors from an :class:`SMCAnalysisResult`.

    Parameters
    ----------
    result : SMCAnalysisResult
        The complete analysis snapshot (single timeframe).
    mtf_aligned : bool
        Whether the multi-timeframe bias agrees.  Passed in by the caller
        who has already compared higher/lower timeframe biases.
    threshold : int
        Minimum ``abs(net_score)`` to set ``meets_threshold=True``.

    Returns
    -------
    ConfluenceScore
    """
    factors: list[ConfluenceFactor] = []
    price = result.current_price

    # ==================================================================
    # TIER 1 — Structure (weight = 3)
    # ==================================================================

    # 1a. Market structure bias
    # Track the bias direction to prevent double-counting with 1b BOS below.
    _bias_direction: str | None = None
    if result.structure and result.structure.bias in (Bias.BULLISH, Bias.BEARISH):
        _bias_direction = result.structure.bias.value  # 'bullish' or 'bearish'
        _add(factors, f"bias_{_bias_direction}", 1, _bias_direction,
             f"Market structure bias is {_bias_direction.upper()}")

    # 1b. Recent BOS → continuation
    # Guard: skip if the BOS direction matches structure_bias already scored in 1a
    # (same structural fact — counting it twice inflates the score by +3).
    if result.structure and result.structure.structure_breaks:
        latest = result.structure.structure_breaks[-1]
        if latest.event in (StructureEvent.BOS_BULL, StructureEvent.BOS_BEAR):
            direction = "bullish" if latest.event == StructureEvent.BOS_BULL else "bearish"
            if direction != _bias_direction:
                _add(factors, f"bos_{direction}", 1, direction,
                     f"Break of Structure ({latest.event.value}) at ${latest.broke_level:,.2f}")

    # 1c. Recent ChOS → reversal
    if result.structure and result.structure.structure_breaks:
        latest = result.structure.structure_breaks[-1]
        if latest.event in (StructureEvent.CHOS_BULL, StructureEvent.CHOS_BEAR):
            direction = "bullish" if latest.event == StructureEvent.CHOS_BULL else "bearish"
            _add(factors, f"chos_{direction}", 1, direction,
                 f"Change of Structure ({latest.event.value}) at ${latest.broke_level:,.2f}")

    # 1d. Nearest S/R within proximity
    if result.nearest_support and _near(result.nearest_support.price, price, SR_PROXIMITY_PCT):
        _add(factors, "sr_support_near", 1, "bullish",
             f"Support at ${result.nearest_support.price:,.2f} "
             f"({result.nearest_support.touches} touches) within {SR_PROXIMITY_PCT*100:.1f}% of price")

    if result.nearest_resistance and _near(result.nearest_resistance.price, price, SR_PROXIMITY_PCT):
        _add(factors, "sr_resistance_near", 1, "bearish",
             f"Resistance at ${result.nearest_resistance.price:,.2f} "
             f"({result.nearest_resistance.touches} touches) within {SR_PROXIMITY_PCT*100:.1f}% of price")

    # 1e. Active order block near price
    if result.active_obs:
        # Pick the closest unbroken OB
        for ob in result.active_obs[:1]:
            if _near(ob.midpoint, price, OB_PROXIMITY_PCT):
                _add(factors, f"ob_{ob.direction}", 1, ob.direction,
                     f"{ob.direction.capitalize()} Order Block "
                     f"${ob.bottom:,.2f}–${ob.top:,.2f} near price")

    # 1e. MTF bias alignment — weight by strength so partial (2/3) alignment
    # contributes proportionally rather than the same flat +3 as full alignment.
    # strength=1.0 → weight 3, strength=0.67 → weight 2, strength=0.33 → weight 1
    if mtf_aligned and mtf_bias is not None:
        raw_w = max(1, round(mtf_bias.strength * TIER_WEIGHTS[1]))
        direction_mtf = mtf_bias.dominant_direction if mtf_bias.dominant_direction in ("bullish", "bearish") else (
            result.structure.bias.value if result.structure else "bullish"
        )
        factors.append(ConfluenceFactor(
            name="mtf_aligned",
            tier=1,
            direction=direction_mtf,
            weight=raw_w,
            description=f"MTF bias aligned ({mtf_bias.summary}) strength={mtf_bias.strength:.2f} → weight {raw_w}",
        ))
    elif mtf_aligned:
        # fallback: no MTFBias object passed, use flat weight
        _add(factors, "mtf_aligned", 1,
             result.structure.bias.value if result.structure else "bullish",
             "Multi-timeframe bias aligned")

    # ==================================================================
    # TIER 2 — Trigger (weight = 2)
    # ==================================================================

    # 2a. Active FVG near price
    if result.active_fvgs:
        for fvg in result.active_fvgs[:1]:
            if _near(fvg.midpoint, price, FVG_PROXIMITY_PCT):
                _add(factors, f"fvg_{fvg.direction}", 2, fvg.direction,
                     f"{fvg.direction.capitalize()} FVG "
                     f"${fvg.bottom:,.2f}–${fvg.top:,.2f} (filled {fvg.fill_pct:.0f}%)")

    # 2b. Liquidity grab + reversal — direction must match the grab implication
    # sell_side grab (stop hunt below lows) → bullish reversal expected
    # buy_side grab (stop hunt above highs) → bearish reversal expected
    if result.recent_grab:
        grab = result.recent_grab
        if grab.type == "sell_side":
            direction = "bullish"   # swept sell-side liquidity → expect bounce up
        elif grab.type == "buy_side":
            direction = "bearish"   # swept buy-side liquidity → expect reversal down
        else:
            direction = "bullish"   # fallback
        _add(factors, f"liquidity_grab_{direction}", 2, direction,
             f"Liquidity grab ({grab.type}) at ${grab.price:,.2f} → {direction} reversal")

    # 2c. Candlestick pattern (most recent)
    if result.candlestick_patterns:
        # Pick the most recent by bar_index
        recent = sorted(result.candlestick_patterns,
                        key=lambda p: p.bar_index, reverse=True)
        pattern = recent[0]
        _add(factors, f"candle_{pattern.direction}", 2, pattern.direction,
             f"Candlestick {pattern.pattern} ({pattern.direction}) "
             f"strength={pattern.strength:.2f}")

    # 2d. Squeeze firing
    if result.squeeze_firing and result.squeeze:
        direction = result.squeeze.direction  # 'bullish' or 'bearish'
        _add(factors, f"squeeze_firing_{direction}", 2, direction,
             f"Squeeze firing — momentum {result.squeeze.momentum_direction} "
             f"({direction})")

    # 2e. Volume climax / surge (most recent)
    if result.volume_signals:
        # Look for climax or rvol_surge type signals
        vol_triggers = [v for v in result.volume_signals
                        if v.type in ("climax", "rvol_surge", "absorption")]
        if vol_triggers:
            v = vol_triggers[0]  # most recent (already sorted desc)
            _add(factors, f"volume_{v.type}_{v.direction}", 2, v.direction,
                 f"Volume {v.type} ({v.direction}) RVOL={v.rvol:.1f}x")

    # 2f. Chart pattern detected
    if result.chart_patterns:
        cp = result.chart_patterns[0]
        _add(factors, f"chart_pattern_{cp.direction}", 2, cp.direction,
             f"Chart pattern: {cp.pattern} ({cp.direction}) "
             f"key level ${cp.key_level:,.2f}")

    # ==================================================================
    # TIER 3 — Confirmation (weight = 1)
    # ==================================================================

    # 3a. Premium / discount zone
    if result.premium_discount:
        zone = result.premium_discount.zone
        if zone == "premium":
            _add(factors, "premium_zone", 3, "bearish",
                 f"Price in PREMIUM zone (above EQ ${result.premium_discount.equilibrium:,.2f})")
        elif zone == "discount":
            _add(factors, "discount_zone", 3, "bullish",
                 f"Price in DISCOUNT zone (below EQ ${result.premium_discount.equilibrium:,.2f})")
        # equilibrium → skip (neutral)

    # 3b. Divergence
    if result.divergences:
        # Take the most recent divergence
        div = result.divergences[-1]  # sorted by bar_index_2 ascending
        # Divergence type contains direction: 'bullish_regular', 'bearish_hidden', etc.
        if "bullish" in div.type:
            direction = "bullish"
        elif "bearish" in div.type:
            direction = "bearish"
        else:
            direction = "bullish"  # fallback
        _add(factors, f"divergence_{direction}", 3, direction,
             f"Divergence: {div.type} ({div.indicator.upper()}) "
             f"strength={div.strength:.2f}")

    # 3c. OBV divergence from volume signals (distinct from price divergence)
    if result.volume_signals:
        obv_divs = [v for v in result.volume_signals if v.type == "obv_divergence"]
        if obv_divs:
            v = obv_divs[0]
            _add(factors, f"obv_divergence_{v.direction}", 3, v.direction,
                 f"OBV divergence ({v.direction}) strength={v.strength:.2f}")

    # 3d. Low-volume pullback from volume signals
    if result.volume_signals:
        lv_pullbacks = [v for v in result.volume_signals if v.type == "low_volume_pullback"]
        if lv_pullbacks:
            v = lv_pullbacks[0]
            _add(factors, f"lv_pullback_{v.direction}", 3, v.direction,
                 f"Low-volume pullback ({v.direction}) RVOL={v.rvol:.2f}x")

    # ==================================================================
    # Aggregate
    # ==================================================================
    bullish_score = sum(f.weight for f in factors if f.direction == "bullish")
    bearish_score = sum(f.weight for f in factors if f.direction == "bearish")
    raw_score = bullish_score + bearish_score
    net_score = bullish_score - bearish_score

    if net_score > 0:
        direction = "bullish"
        dominant = "bullish"
    elif net_score < 0:
        direction = "bearish"
        dominant = "bearish"
    else:
        direction = "neutral"
        dominant = "neutral"

    return ConfluenceScore(
        direction=direction,
        raw_score=raw_score,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        net_score=net_score,
        factors=factors,
        meets_threshold=abs(net_score) >= threshold,
        dominant_direction=dominant,
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def score_to_dict(score: ConfluenceScore) -> dict:
    """Convert a :class:`ConfluenceScore` to a JSON-serialisable dict.

    Returns
    -------
    dict
        Contains all scalar fields plus ``factors`` as a list of dicts.
    """
    return {
        "direction": score.direction,
        "raw_score": score.raw_score,
        "bullish_score": score.bullish_score,
        "bearish_score": score.bearish_score,
        "net_score": score.net_score,
        "meets_threshold": score.meets_threshold,
        "dominant_direction": score.dominant_direction,
        "factor_count": len(score.factors),
        "factors": [
            {
                "name": f.name,
                "tier": f.tier,
                "direction": f.direction,
                "weight": f.weight,
                "description": f.description,
            }
            for f in score.factors
        ],
    }


def score_to_summary(score: ConfluenceScore) -> str:
    """Produce a human-readable summary of a :class:`ConfluenceScore`.

    The output groups factors by tier and shows the net score and direction.

    Returns
    -------
    str
        Multi-line summary suitable for logging or LLM prompts.
    """
    lines: list[str] = []
    arrow = "🟢" if score.direction == "bullish" else ("🔴" if score.direction == "bearish" else "⚪")
    lines.append(f"{arrow} Confluence: {score.direction.upper()}  "
                 f"(net={score.net_score:+d}, raw={score.raw_score})")
    lines.append(f"   Bullish={score.bullish_score}  Bearish={score.bearish_score}  "
                 f"Threshold={'✅ MET' if score.meets_threshold else '❌ NOT MET'}")
    lines.append("")

    # Group factors by tier
    tier_names = {1: "Tier 1 — Structure (3×)", 2: "Tier 2 — Trigger (2×)", 3: "Tier 3 — Confirmation (1×)"}
    for tier in (1, 2, 3):
        tier_factors = [f for f in score.factors if f.tier == tier]
        if not tier_factors:
            continue
        lines.append(f"  {tier_names[tier]}:")
        for f in tier_factors:
            icon = "▲" if f.direction == "bullish" else "▼"
            lines.append(f"    {icon} [{f.direction.upper():>7s} +{f.weight}]  {f.description}")
        lines.append("")

    if not score.factors:
        lines.append("  (no confluence factors detected)")

    return "\n".join(lines)
