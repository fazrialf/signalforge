"""Multi-timeframe bias alignment checker for SignalForge.

This module evaluates whether the 1D, 4H, and 1H market structure biases
are aligned, providing a unified MTFBias result with strength scoring and
a human-readable summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.market_structure import Bias
from signals.pipeline import SMCAnalysisResult


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class MTFBias:
    """Encapsulates the multi-timeframe bias alignment across 4H, 1H, and 15m.

    Attributes:
        daily_bias: Bias extracted from the daily (1D) timeframe (kept for reference).
        h4_bias: Bias extracted from the 4-hour (4H) timeframe.
        h1_bias: Bias extracted from the 1-hour (1H) timeframe.
        m15_bias: Bias extracted from the 15-minute (15m) timeframe.
        aligned: True when all three scalping TFs (4H/1H/15m) agree on direction.
        dominant_direction: 'bullish', 'bearish', or 'conflicting'.
        strength: Alignment strength from 0.0 to 1.0.
            1.0  → all 3 aligned
            0.67 → 2 out of 3 agree
            0.33 → only 1 directional vote (others neutral/unknown)
            0.0  → no dominant direction or all neutral/unknown
        summary: Human-readable one-liner describing the MTF alignment state.
    """

    daily_bias: Bias
    h4_bias: Bias
    h1_bias: Bias
    m15_bias: Bias = Bias.UNKNOWN
    aligned: bool = False
    dominant_direction: str = "conflicting"
    strength: float = 0.0
    summary: str = ""


# ---------------------------------------------------------------------------
# Neutral bias helper
# ---------------------------------------------------------------------------

# Biases that carry no directional vote (neutral for alignment counting).
_NEUTRAL_BIASES: frozenset[Bias] = frozenset({Bias.UNKNOWN, Bias.RANGING})


def _is_neutral(bias: Bias) -> bool:
    """Return True if *bias* should be treated as neutral (non-directional)."""
    return bias in _NEUTRAL_BIASES


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def check_mtf_bias(results: dict[str, SMCAnalysisResult]) -> MTFBias:
    """Compute multi-timeframe bias alignment from analysis results.

    Expects *results* to contain keys ``'1d'``, ``'4h'``, and ``'1h'``
    mapping to :class:`~signals.pipeline.SMCAnalysisResult` instances.  Any
    missing key, or a result whose ``.structure`` attribute is ``None``, is
    treated as :attr:`~core.market_structure.Bias.UNKNOWN`.

    Alignment rules
    ---------------
    * **All 3 BULLISH** → ``aligned=True``, ``dominant='bullish'``, ``strength=1.0``
    * **All 3 BEARISH** → ``aligned=True``, ``dominant='bearish'``, ``strength=1.0``
    * **2/3 agree (BULLISH or BEARISH)** → ``aligned=False``, ``dominant=majority``,
      ``strength=0.67``
    * **1/3 directional, rest neutral** → ``aligned=False``, ``dominant=that direction``,
      ``strength=0.33``
    * **All neutral/unknown, or no majority** → ``aligned=False``,
      ``dominant='conflicting'``, ``strength=0.0``

    RANGING and UNKNOWN count as neutral and do not vote for either direction.

    Args:
        results: Mapping of timeframe labels to
            :class:`~signals.pipeline.SMCAnalysisResult` objects.

    Returns:
        A fully populated :class:`MTFBias` instance.
    """
    # -- Extract biases, falling back to UNKNOWN when data is absent ----------
    def _extract(key: str) -> Bias:
        result = results.get(key)
        if result is None:
            return Bias.UNKNOWN
        structure = getattr(result, "structure", None)
        if structure is None:
            return Bias.UNKNOWN
        bias = getattr(structure, "bias", Bias.UNKNOWN)
        return bias if isinstance(bias, Bias) else Bias.UNKNOWN

    daily_bias = _extract("1d")
    h4_bias    = _extract("4h")
    h1_bias    = _extract("1h")

    # Scalping MTF stack: 4h → 1h → 15m (1d is too slow for scalp entries)
    # We still read all TFs but weight the scalping-relevant ones
    m15_bias   = _extract("15m")

    # Use 4h/1h/15m for alignment — drop 1d for scalping
    biases = [h4_bias, h1_bias, m15_bias]
    tf_labels = {"4h": h4_bias, "1h": h1_bias, "15m": m15_bias}

    # -- Count directional votes ----------------------------------------------
    bullish_count = sum(1 for b in biases if b is Bias.BULLISH)
    bearish_count = sum(1 for b in biases if b is Bias.BEARISH)
    total_directional = bullish_count + bearish_count

    # -- Determine alignment, dominant direction, and strength ----------------
    if bullish_count == 3:
        aligned           = True
        dominant_direction = "bullish"
        strength           = 1.0
    elif bearish_count == 3:
        aligned           = True
        dominant_direction = "bearish"
        strength           = 1.0
    elif bullish_count == 2 and bearish_count <= 1:
        aligned           = False
        dominant_direction = "bullish"
        strength           = 0.67
    elif bearish_count == 2 and bullish_count <= 1:
        aligned           = False
        dominant_direction = "bearish"
        strength           = 0.67
    elif bullish_count == 1 and bearish_count == 0:
        # One directional vote, rest are neutral
        aligned           = False
        dominant_direction = "bullish"
        strength           = 0.33
    elif bearish_count == 1 and bullish_count == 0:
        # One directional vote, rest are neutral
        aligned           = False
        dominant_direction = "bearish"
        strength           = 0.33
    else:
        # Conflicting (e.g. 1 bull, 1 bear, 1 neutral) or all neutral
        aligned           = False
        dominant_direction = "conflicting"
        strength           = 0.0

    # -- Build human-readable summary -----------------------------------------
    summary = _build_summary(
        aligned=aligned,
        dominant_direction=dominant_direction,
        h4_bias=h4_bias,
        h1_bias=h1_bias,
        m15_bias=m15_bias,
    )

    return MTFBias(
        daily_bias=daily_bias,
        h4_bias=h4_bias,
        h1_bias=h1_bias,
        m15_bias=m15_bias,
        aligned=aligned,
        dominant_direction=dominant_direction,
        strength=round(strength, 2),
        summary=summary,
    )


def _build_summary(
    *,
    aligned: bool,
    dominant_direction: str,
    h4_bias: Bias,
    h1_bias: Bias,
    m15_bias: Bias,
) -> str:
    """Return a human-readable one-liner describing the MTF alignment state.

    Args:
        aligned: Whether all three scalping TFs (4H/1H/15m) are in agreement.
        dominant_direction: 'bullish', 'bearish', or 'conflicting'.
        h4_bias: Bias from the 4H timeframe.
        h1_bias: Bias from the 1H timeframe.
        m15_bias: Bias from the 15m timeframe.

    Returns:
        A concise summary string with an emoji prefix.
    """
    if aligned and dominant_direction == "bullish":
        return "✅ MTF ALIGNED BULLISH (4H+1H+15m)"

    if aligned and dominant_direction == "bearish":
        return "✅ MTF ALIGNED BEARISH (4H+1H+15m)"

    if dominant_direction == "conflicting":
        return "❌ MTF CONFLICTING — no dominant direction"

    # Mixed — show per-timeframe breakdown
    h4_str  = h4_bias.name
    h1_str  = h1_bias.name
    m15_str = m15_bias.name
    return (
        f"⚠️ MTF MIXED: 4H={h4_str}, 1H={h1_str}, 15m={m15_str}"
    )


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def mtf_bias_to_dict(bias: MTFBias) -> dict:
    """Serialise a :class:`MTFBias` instance to a plain dictionary.

    Bias enum members are converted to their string names so the result is
    JSON-serialisable without further processing.

    Args:
        bias: The :class:`MTFBias` instance to serialise.

    Returns:
        A dictionary with string-typed bias values and all scalar fields.

    Example::

        >>> d = mtf_bias_to_dict(result)
        >>> d['dominant_direction']
        'bullish'
    """
    return {
        "daily_bias":          bias.daily_bias.name,
        "h4_bias":             bias.h4_bias.name,
        "h1_bias":             bias.h1_bias.name,
        "m15_bias":            bias.m15_bias.name,
        "aligned":             bias.aligned,
        "dominant_direction":  bias.dominant_direction,
        "strength":            bias.strength,
        "summary":             bias.summary,
    }
