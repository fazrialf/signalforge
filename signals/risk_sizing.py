"""signals/risk_sizing.py — Position sizing and TP validation for SignalForge.

Provides:
- ``calc_position_size``: Scales dollar risk by LLM confidence and computes
  the number of units to trade given an entry and stop-loss price.
- ``validate_tp_structure``: Sanity-checks TP1/TP2/TP3 ordering and proximity
  to nearest support/resistance levels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from signals.llm_engine import SignalResult
from config.settings import BASE_RISK_PCT, MID_RISK_PCT, HIGH_RISK_PCT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PositionSize:
    """Encapsulates the calculated position size for a single trade.

    Attributes:
        symbol: Asset ticker, e.g. ``'BTC/USDT'``.
        side: Trade direction — ``'LONG'`` or ``'SHORT'``.
        entry: Entry price in quote currency.
        stop_loss: Stop-loss price in quote currency.
        size: Number of base-asset units to trade.
        risk_usd: Dollar amount at risk (``account_balance * risk_pct / 100``).
        risk_pct: Percentage of account being risked (1.0, 1.5, or 2.0).
        notional_usd: Total position value in USD (``size * entry``).
    """

    symbol: str
    side: str          # 'LONG' or 'SHORT'
    entry: float
    stop_loss: float
    size: float        # units to trade
    risk_usd: float    # dollar risk
    risk_pct: float    # % of account (1.0, 1.5, or 2.0)
    notional_usd: float  # size * entry


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def calc_position_size(
    account_balance: float,
    confidence: float,
    entry: float,
    stop_loss: float,
    side: str,
    symbol: str = "BTC/USDT",
    min_risk_pct: float = 1.0,
    max_risk_pct: float = 2.0,
) -> PositionSize:
    """Calculate a risk-scaled position size.

    Risk percentage is linearly stepped based on LLM confidence:

    +-----------------+-----------+
    | Confidence      | Risk %    |
    +=================+===========+
    | 75 – 79         | 1.0%      |
    +-----------------+-----------+
    | 80 – 89         | 1.5%      |
    +-----------------+-----------+
    | 90+             | 2.0%      |
    +-----------------+-----------+

    ``min_risk_pct`` and ``max_risk_pct`` are respected as hard clamps,
    so callers can override the tier boundaries if needed.

    Formula::

        risk_usd = account_balance * risk_pct / 100
        size     = risk_usd / abs(entry - stop_loss)

    Args:
        account_balance: Total account equity in USD.
        confidence: LLM confidence score (0–100).
        entry: Intended entry price.
        stop_loss: Stop-loss price.
        side: ``'LONG'`` or ``'SHORT'``.
        symbol: Asset ticker (informational only, stored in the dataclass).
        min_risk_pct: Floor for risk percentage (default 1.0).
        max_risk_pct: Ceiling for risk percentage (default 2.0).

    Returns:
        A populated :class:`PositionSize` dataclass.

    Raises:
        ValueError: If ``entry == stop_loss`` (zero-distance stop).
    """
    # Determine risk tier from settings constants
    if confidence >= 90:
        risk_pct = HIGH_RISK_PCT
    elif confidence >= 80:
        risk_pct = MID_RISK_PCT
    else:
        risk_pct = BASE_RISK_PCT

    # Honour caller-supplied clamps
    risk_pct = max(min_risk_pct, min(max_risk_pct, risk_pct))

    stop_distance = abs(entry - stop_loss)
    if stop_distance == 0:
        raise ValueError(
            f"entry ({entry}) and stop_loss ({stop_loss}) are identical — "
            "cannot compute position size with a zero-distance stop."
        )

    risk_usd = account_balance * risk_pct / 100.0
    size = risk_usd / stop_distance
    notional_usd = size * entry

    logger.debug(
        "[RiskSizing] %s %s | conf=%.0f%% risk=%.1f%% "
        "risk_usd=$%.2f size=%.6f notional=$%.2f",
        side, symbol, confidence, risk_pct, risk_usd, size, notional_usd,
    )

    return PositionSize(
        symbol=symbol,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        size=size,
        risk_usd=risk_usd,
        risk_pct=risk_pct,
        notional_usd=notional_usd,
    )


# ---------------------------------------------------------------------------
# TP structure validation
# ---------------------------------------------------------------------------

def validate_tp_structure(
    signal: SignalResult,
    nearest_support: Optional[float] = None,
    nearest_resistance: Optional[float] = None,
) -> dict:
    """Validate TP1/TP2/TP3 ordering and proximity to key S/R levels.

    Checks performed:
    1. TP ordering is logical (TP1 < TP2 < TP3 for LONG; reversed for SHORT).
    2. No TP is within 0.5% of the entry price (too tight to be useful).
    3. For LONG signals, no TP lies beyond the nearest resistance level.
    4. For SHORT signals, no TP lies below the nearest support level.

    Warnings are non-fatal — the caller decides whether to block the signal.

    Args:
        signal: Parsed ``SignalResult`` containing ``tp1``/``tp2``/``tp3``
            and ``entry``/``signal`` fields.
        nearest_support: Nearest identified support price, or ``None``.
        nearest_resistance: Nearest identified resistance price, or ``None``.

    Returns:
        A dict with two keys:

        - ``valid`` (bool): ``True`` if no blocking issues were found.
          Currently all issues are warnings, so this is always ``True``
          (reserved for future hard-fail conditions).
        - ``warnings`` (list[str]): Human-readable warning messages.
    """
    warnings: list[str] = []
    entry = signal.entry
    tps = [signal.tp1, signal.tp2, signal.tp3]
    is_long = signal.signal == "BUY"

    # 1. Ordering check
    if is_long:
        if not (signal.tp1 < signal.tp2 < signal.tp3):
            warnings.append(
                f"TP ordering invalid for LONG: "
                f"TP1={signal.tp1}, TP2={signal.tp2}, TP3={signal.tp3} "
                f"(expected TP1 < TP2 < TP3)"
            )
    elif signal.signal == "SELL":
        if not (signal.tp1 > signal.tp2 > signal.tp3):
            warnings.append(
                f"TP ordering invalid for SHORT: "
                f"TP1={signal.tp1}, TP2={signal.tp2}, TP3={signal.tp3} "
                f"(expected TP1 > TP2 > TP3)"
            )

    # 2. Proximity to entry (<0.5% move)
    if entry and entry != 0:
        for i, tp in enumerate(tps, start=1):
            if tp and tp != 0:
                move_pct = abs(tp - entry) / entry * 100
                if move_pct < 0.5:
                    warnings.append(
                        f"TP{i} ({tp}) is less than 0.5% from entry ({entry}) — "
                        f"move too small ({move_pct:.2f}%)"
                    )

    # 3 & 4. TP beyond nearest S/R
    if is_long and nearest_resistance is not None:
        for i, tp in enumerate(tps, start=1):
            if tp and tp > nearest_resistance:
                warnings.append(
                    f"TP{i} ({tp}) is beyond nearest resistance ({nearest_resistance}) — "
                    "price may not reach this level"
                )

    if signal.signal == "SELL" and nearest_support is not None:
        for i, tp in enumerate(tps, start=1):
            if tp and tp < nearest_support:
                warnings.append(
                    f"TP{i} ({tp}) is below nearest support ({nearest_support}) — "
                    "price may not reach this level"
                )

    if warnings:
        logger.debug("[RiskSizing] TP validation warnings: %s", warnings)

    return {"valid": True, "warnings": warnings}
