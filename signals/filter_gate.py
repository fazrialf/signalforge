"""signals/filter_gate.py — 10-filter quality gate for SignalForge.

Blocks low-quality signals before Telegram delivery.  All 10 filters run in
order; the first failure short-circuits and returns a ``FilterResult`` with
``passed=False``.  Filters 7–10 are stubs that always pass (Sprint 6 will
wire in news, volatility, fear/greed, and spread data).
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from signals.llm_engine import SignalResult
from signals.mtf_bias import MTFBias
from signals.cooldown import CooldownTracker
from config.settings import (
    MIN_LLM_CONFIDENCE,
    MIN_RR_RATIO,
    MAX_CONCURRENT,
    MAX_PORTFOLIO_HEAT_PCT,
    DAILY_LOSS_LIMIT_PCT,
    ACCOUNT_BALANCE,
)
from config.settings import (
    MTF_STRENGTH_MIN_SWING,
    MTF_STRENGTH_MIN_SCALP,
    SESSION_FILTER_ENABLED,
    SESSION_ACTIVE_START_UTC,
    SESSION_ACTIVE_END_UTC,
    TIER1_ASSETS,
    PRIMARY_TF,
    ATR_PERIOD,
    ATR_AVG_PERIOD,
    ATR_SPIKE_MULTIPLIER,
    ATR_MIN_BARS,
)
try:
    from external.fear_greed import fetch_fear_greed
    from external.economic_calendar import is_near_high_impact_event
    _EXTERNAL_DATA_AVAILABLE = True
except ImportError:
    _EXTERNAL_DATA_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class FilterResult:
    """Result returned by :meth:`FilterGate.apply`.

    Attributes:
        passed: ``True`` if the signal cleared all filters.
        reason: Human-readable explanation of why the signal was blocked.
            Empty string when ``passed=True``.
        filter_name: Identifier of the filter that blocked the signal
            (e.g. ``'filter_1_confidence'``).  Empty string when
            ``passed=True``.
    """

    passed: bool
    reason: str         # empty when passed
    filter_name: str    # empty when passed


# ---------------------------------------------------------------------------
# Filter gate
# ---------------------------------------------------------------------------

class FilterGate:
    """Applies 10 sequential quality filters to a signal.

    Filters are evaluated in order.  The first failure immediately returns a
    ``FilterResult(passed=False, ...)``.  If all filters pass the returned
    result has ``passed=True``.

    Active filters (Sprint 5):

    1. Confidence ≥ threshold
    2. R:R ratio ≥ minimum
    3. MTF bias aligned
    4. Asset not in cooldown
    5. Max concurrent active signals not exceeded
    6. Portfolio heat (sum of open risk %) < maximum

    Stub filters (Sprint 6 — always pass):

    7. No high-impact news within 2 hours
    8. Volatility regime acceptable
    9. Fear & Greed index not extreme
    10. Spread not abnormally wide

    Args:
        cooldown_tracker: Shared :class:`~signals.cooldown.CooldownTracker`
            instance used for filter 4.
        config: Optional dict to override default thresholds.  Supported keys:
            ``min_confidence``, ``min_rr``, ``max_active_signals``,
            ``max_heat``.
    """

    def __init__(
        self,
        cooldown_tracker: CooldownTracker,
        config: Optional[dict] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self.cooldown = cooldown_tracker
        self.config: dict = config or {}
        self._db_path: Optional[str] = db_path

        # Thresholds — prefer config dict, fall back to settings
        self.min_confidence: float = self.config.get("min_confidence", MIN_LLM_CONFIDENCE)
        self.min_rr: float = self.config.get("min_rr", MIN_RR_RATIO)
        self.max_active_signals: int = self.config.get("max_active_signals", MAX_CONCURRENT)
        self.max_portfolio_heat_pct: float = self.config.get("max_heat", MAX_PORTFOLIO_HEAT_PCT)

        # Fear & Greed cache — pre-fetched async in main.py and injected here
        # to avoid a blocking sync HTTP call inside the async pipeline.
        self._fear_greed_cache: Optional[dict] = None

    def set_fear_greed(self, fg: dict) -> None:
        """Inject a pre-fetched Fear & Greed result for filter 9."""
        self._fear_greed_cache = fg

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(
        self,
        signal: SignalResult,
        mtf_bias: MTFBias,
        symbol: str,
        active_positions: Optional[list[dict]] = None,
        current_price: float = 0.0,
        candles: Optional["pd.DataFrame"] = None,
    ) -> FilterResult:
        """Run all 10 filters against *signal* and return the verdict.

        Args:
            signal: Parsed ``SignalResult`` from the LLM engine.
            mtf_bias: Multi-timeframe bias result from
                :func:`~signals.mtf_bias.check_mtf_bias`.
            symbol: Asset ticker, e.g. ``'BTC/USDT'``.
            active_positions: List of currently open position dicts.  Each
                dict must contain at least ``{'symbol': str, 'side': str,
                'risk_pct': float}``.  Defaults to an empty list.
            current_price: Current market price.
            candles: Optional DataFrame with columns ``high``, ``low``,
                ``close`` used by filter 8 (ATR volatility).  When ``None``
                filter 8 is skipped (fail-safe pass).

        Returns:
            :class:`FilterResult` — ``passed=True`` only when every filter
            passes.
        """
        active_positions = active_positions or []

        result = (
            self._f1_confidence(signal)
            or self._f2_rr_ratio(signal)
            or self._f11_session_filter(symbol)   # M-1: moved up — cheap time check before heavy filters
            or self._f3_mtf_aligned(signal, mtf_bias)
            or self._f4_cooldown(symbol)
            or self._f5_max_active(active_positions)
            or self._f6_portfolio_heat(active_positions)
            or self._f6b_daily_loss_limit(active_positions)
            or self._f7_news_stub()
            or self._f8_atr_volatility(candles)
            or self._f9_fear_greed_stub()
            or self._f10_spread_stub()
        )

        if result is not None:
            logger.info(
                "[FilterGate] BLOCKED %s | filter=%s | reason=%s",
                symbol, result.filter_name, result.reason,
            )
            return result

        logger.info("[FilterGate] PASSED all filters for %s", symbol)
        return FilterResult(passed=True, reason="", filter_name="")

    # ------------------------------------------------------------------
    # Individual filters (return None on pass, FilterResult on fail)
    # ------------------------------------------------------------------

    def _f1_confidence(self, signal: SignalResult) -> Optional[FilterResult]:
        """Filter 1: LLM confidence must meet the minimum threshold."""
        if signal.signal == "PASS":
            return FilterResult(
                passed=False,
                reason="Signal is PASS — no actionable direction from LLM",
                filter_name="filter_1_confidence",
            )
        if signal.confidence < self.min_confidence:
            return FilterResult(
                passed=False,
                reason=(
                    f"Confidence {signal.confidence:.0f}% below minimum "
                    f"{self.min_confidence:.0f}%"
                ),
                filter_name="filter_1_confidence",
            )
        return None

    def _f2_rr_ratio(self, signal: SignalResult) -> Optional[FilterResult]:
        """Filter 2: Risk-to-reward ratio must meet the minimum threshold."""
        if signal.rr_ratio < self.min_rr:
            return FilterResult(
                passed=False,
                reason=(
                    f"R:R {signal.rr_ratio:.2f} below minimum {self.min_rr:.2f}"
                ),
                filter_name="filter_2_rr_ratio",
            )
        return None

    def _f3_mtf_aligned(
        self, signal: SignalResult, mtf_bias: MTFBias
    ) -> Optional[FilterResult]:
        """Filter 3: MTF bias direction must match signal, with sufficient strength.

        Strength thresholds (from settings):
          - Scalping (5m primary): 0.33 — accepts 1/3 TFs aligned (5m can lead)
          - Swing (1h+ primary):   0.67 — requires 2/3 TFs aligned
          - 1.0 (old binary) was too strict — filtered valid swing setups where
            4H+1H agree but 15m is neutral.
        """
        expected_direction = "bullish" if signal.signal == "BUY" else "bearish"
        # Pick threshold based on primary timeframe
        min_strength = (
            MTF_STRENGTH_MIN_SCALP if PRIMARY_TF in ("1m", "5m")
            else MTF_STRENGTH_MIN_SWING
        )
        direction_ok = mtf_bias.dominant_direction == expected_direction
        strength_ok  = mtf_bias.strength >= min_strength
        if not direction_ok or not strength_ok:
            return FilterResult(
                passed=False,
                reason=(
                    f"MTF bias not aligned with {signal.signal}: "
                    f"dominant={mtf_bias.dominant_direction}, "
                    f"strength={mtf_bias.strength:.2f} (min={min_strength:.2f}) | "
                    f"{mtf_bias.summary}"
                ),
                filter_name="filter_3_mtf_aligned",
            )
        return None

    def _f4_cooldown(self, symbol: str) -> Optional[FilterResult]:
        """Filter 4: Asset must not be within its post-signal cooldown period."""
        if self.cooldown.is_in_cooldown(symbol):
            remaining = self.cooldown.time_remaining(symbol)
            return FilterResult(
                passed=False,
                reason=f"{symbol} in cooldown — {remaining} min remaining",
                filter_name="filter_4_cooldown",
            )
        return None

    def _f5_max_active(
        self, active_positions: list[dict]
    ) -> Optional[FilterResult]:
        """Filter 5: Number of open positions must not exceed the maximum."""
        count = len(active_positions)
        if count >= self.max_active_signals:
            return FilterResult(
                passed=False,
                reason=(
                    f"Max active signals reached: {count}/{self.max_active_signals}"
                ),
                filter_name="filter_5_max_active",
            )
        return None

    def _f6_portfolio_heat(
        self, active_positions: list[dict]
    ) -> Optional[FilterResult]:
        """Filter 6: Total portfolio risk must stay below the heat ceiling.

        Portfolio heat is the sum of ``risk_pct`` across all open positions.
        """
        heat = sum(float(p.get("risk_pct", 0)) for p in active_positions)
        if heat >= self.max_portfolio_heat_pct:
            return FilterResult(
                passed=False,
                reason=(
                    f"Portfolio heat {heat:.1f}% at or above maximum "
                    f"{self.max_portfolio_heat_pct:.1f}%"
                ),
                filter_name="filter_6_portfolio_heat",
            )
        return None

    # ------------------------------------------------------------------
    # Stub filters (Sprint 6)
    # ------------------------------------------------------------------

    def _f7_news_stub(self) -> Optional[FilterResult]:
        """Filter 7: High-impact news proximity check.

        Blocks signals within 2 hours of a known high-impact macro event
        (CPI, FOMC, NFP). Requires external.economic_calendar module.
        """
        if not _EXTERNAL_DATA_AVAILABLE:
            return None
        try:
            if is_near_high_impact_event(hours_threshold=2):
                return FilterResult(
                    passed=False,
                    reason="High-impact macro event within 2 hours (CPI/FOMC/NFP)",
                    filter_name="filter_7_news",
                )
        except Exception as e:
            logger.warning("[FilterGate] F7 news check failed: %s — skipping", e)
        return None

    def _f8_atr_volatility(self, candles: Optional[pd.DataFrame]) -> Optional[FilterResult]:
        """Filter 8: Block signals during abnormal ATR spike regimes.

        Logic:
          - Compute ATR(ATR_PERIOD) using Wilder's smoothed true range.
          - Compute a rolling mean of ATR over ATR_AVG_PERIOD bars as the baseline.
          - If the most recent ATR > ATR_SPIKE_MULTIPLIER × baseline → block.
          - If candles is None or history too short → skip (fail-safe pass).

        This catches flash-crash / news-spike conditions where the spread
        between TP and SL is meaningless because price is in free-fall or
        vertical pump — exactly when the LLM is most likely to hallucinate
        a high-confidence entry.
        """
        if candles is None or len(candles) < ATR_MIN_BARS:
            return None  # not enough data — safe to pass

        try:
            high  = candles["high"].astype(float)
            low   = candles["low"].astype(float)
            close = candles["close"].astype(float)

            # True Range: max of three measures
            prev_close = close.shift(1)
            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low  - prev_close).abs(),
            ], axis=1).max(axis=1)

            # Wilder's smoothed ATR (equivalent to EWM with alpha = 1/period)
            atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()

            # Baseline: rolling mean of ATR over ATR_AVG_PERIOD bars
            atr_avg = atr.rolling(ATR_AVG_PERIOD).mean()

            current_atr = float(atr.iloc[-1])
            baseline    = float(atr_avg.iloc[-1])

            if baseline <= 0:
                return None  # degenerate — skip

            ratio = current_atr / baseline
            if ratio > ATR_SPIKE_MULTIPLIER:
                return FilterResult(
                    passed=False,
                    reason=(
                        f"ATR spike detected: current ATR {current_atr:.5f} is "
                        f"{ratio:.1f}× the {ATR_AVG_PERIOD}-bar average "
                        f"{baseline:.5f} (threshold {ATR_SPIKE_MULTIPLIER}×) — "
                        f"abnormal volatility regime, signal unreliable"
                    ),
                    filter_name="filter_8_atr_volatility",
                )
        except Exception as e:
            logger.warning("[FilterGate] F8 ATR check failed: %s — skipping", e)

        return None

    def _f11_session_filter(self, symbol: str) -> Optional[FilterResult]:
        """Filter 11: Block alt-coin signals outside London/NY active session.

        BTC and ETH (TIER1_ASSETS) trade 24/7 — no session restriction.
        All other assets are blocked outside 07:00–22:00 UTC to avoid the
        low-liquidity Asian session where spreads are 2–3× wider and false
        liquidity sweeps are common.

        Window: London open (07:00 UTC) → NY close (22:00 UTC).
        Configurable via SESSION_ACTIVE_START_UTC / SESSION_ACTIVE_END_UTC.
        """
        import datetime
        if not SESSION_FILTER_ENABLED:
            return None
        if symbol in TIER1_ASSETS:
            return None  # BTC/ETH always allowed
        hour_utc = datetime.datetime.now(datetime.timezone.utc).hour
        if not (SESSION_ACTIVE_START_UTC <= hour_utc < SESSION_ACTIVE_END_UTC):
            return FilterResult(
                passed=False,
                reason=(
                    f"{symbol} outside active session window "
                    f"({SESSION_ACTIVE_START_UTC:02d}:00–{SESSION_ACTIVE_END_UTC:02d}:00 UTC) "
                    f"— current hour: {hour_utc:02d}:00 UTC"
                ),
                filter_name="filter_11_session",
            )
        return None

    def _f9_fear_greed_stub(self) -> Optional[FilterResult]:
        """Filter 9: Fear & Greed index extremes check.

        Blocks signals when market sentiment is at dangerous extremes
        (value < 10 = Extreme Fear, > 90 = Extreme Greed).
        Uses pre-fetched cache injected via set_fear_greed() to avoid
        blocking the async pipeline with a sync HTTP call.
        """
        if not _EXTERNAL_DATA_AVAILABLE:
            return None
        fg = self._fear_greed_cache
        if fg is None:
            return None  # No data available — skip filter rather than block
        try:
            if fg.get("is_extreme", False):
                val = fg.get("value", 50)
                cls = fg.get("classification", "Unknown")
                return FilterResult(
                    passed=False,
                    reason=f"Fear & Greed at extreme: {val} ({cls})",
                    filter_name="filter_9_fear_greed",
                )
        except Exception as e:
            logger.warning("[FilterGate] F9 fear/greed check failed: %s — skipping", e)
        return None

    def _f10_spread_stub(self) -> Optional[FilterResult]:
        """Filter 10 (stub): Bid-ask spread width check.

        Always passes until Sprint 7 adds live order-book spread monitoring.
        """
        # TODO Sprint 7: block if (ask - bid) / mid_price > MAX_SPREAD_PCT
        return None

    def _f6b_daily_loss_limit(
        self, active_positions: list[dict]
    ) -> Optional[FilterResult]:
        """Circuit breaker: block new signals if daily realised loss exceeds limit.

        Queries the paper_trades table for today's closed trades with negative
        pnl_usd.  Falls back to scanning active_positions when no db_path was
        supplied (unit-test / no-DB mode).

        Blocks when total loss >= DAILY_LOSS_LIMIT_PCT of ACCOUNT_BALANCE.
        """
        daily_limit_usd = ACCOUNT_BALANCE * (DAILY_LOSS_LIMIT_PCT / 100)

        if self._db_path:
            # Query DB for today's realised losses — this is the only reliable
            # source because active_positions only holds OPEN trades.
            today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            try:
                with sqlite3.connect(self._db_path, timeout=5) as conn:
                    rows = conn.execute(
                        """
                        SELECT COALESCE(SUM(pnl_usd), 0.0)
                        FROM   paper_trades
                        WHERE  status = 'closed'
                          AND  pnl_usd < 0
                          AND  DATE(closed_at) = ?
                        """,
                        (today_str,),
                    ).fetchone()
                today_loss = rows[0] if rows else 0.0
            except Exception as exc:
                logger.warning("[FilterGate] daily-loss DB query failed: %s", exc)
                today_loss = 0.0
        else:
            # No DB available — fall back to active_positions (unit-test mode).
            today_loss = sum(
                p.get("realised_pnl", 0.0)
                for p in active_positions
                if p.get("realised_pnl", 0.0) < 0
            )

        if abs(today_loss) >= daily_limit_usd:
            return FilterResult(
                passed=False,
                reason=(
                    f"Daily loss limit reached: ${abs(today_loss):.2f} >= "
                    f"${daily_limit_usd:.2f} ({DAILY_LOSS_LIMIT_PCT}% of balance)"
                ),
                filter_name="filter_6b_daily_loss_limit",
            )
        return None
