"""signals/bos_retest.py — BOS Retest Entry State Machine (S-3)

Implements the ICT/SMC pullback entry model:

  IDLE → ARMED → TRIGGERED

1. **IDLE**: No recent BOS. Confluence threshold fires → LLM fires immediately
   (legacy behaviour, unchanged).

2. **ARMED**: A fresh BOS or ChOS is detected on the primary TF. The engine
   records the impulse candle's FVG/OB zones and waits. The LLM is *suppressed*
   until a retest occurs or the watcher expires.

3. **TRIGGERED**: Price taps into the FVG or OB left by the impulse candle AND
   a valid reversal candle prints at that zone. The LLM is allowed to fire.

Design principles:
- Per-symbol state, no DB — fully in-memory, resets on restart.
- Works entirely from existing ``SMCAnalysisResult`` output. Zero changes to
  ``market_structure.py``, ``pipeline.py``, or ``confluence.py``.
- Falls back to IDLE (legacy immediate fire) when no FVG/OB is available near
  the BOS impulse — prevents missed signals on clean breakouts with no retest
  zone.
- TTL-based expiry prevents stale ARMED state from blocking signals indefinitely.
- All thresholds configurable via ``config/settings.py``.

Usage in main.py::

    from signals.bos_retest import BOSRetestWatcher

    bos_watcher = BOSRetestWatcher()          # shared instance, lives in main()

    # Inside the per-symbol analysis loop, BEFORE the LLM call:
    retest_ok, retest_reason = bos_watcher.update(
        symbol=symbol,
        primary_r=primary_r,          # SMCAnalysisResult for primary TF
        current_price=price,
    )
    if not retest_ok:
        logger.info("[BOS_RETEST] %s suppressed: %s", symbol, retest_reason)
        continue   # skip LLM this cycle
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from signals.pipeline import SMCAnalysisResult
from core.market_structure import StructureEvent
from core.smc import FairValueGap, OrderBlock
from config.settings import (
    BOS_RETEST_ENABLED,
    BOS_RETEST_TTL_BARS,
    BOS_RETEST_FVG_TOLERANCE,
    BOS_RETEST_OB_TOLERANCE,
    BOS_RETEST_REQUIRE_REVERSAL_CANDLE,
    PRIMARY_TF,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class RetestState(str, Enum):
    IDLE      = "IDLE"       # no pending BOS — fire LLM immediately
    ARMED     = "ARMED"      # BOS detected, waiting for pullback
    TRIGGERED = "TRIGGERED"  # retest tapped — allow LLM this cycle then reset


# ---------------------------------------------------------------------------
# Per-symbol watcher state
# ---------------------------------------------------------------------------

@dataclass
class _SymbolState:
    state:           RetestState     = RetestState.IDLE
    bos_direction:   str             = ""       # "bullish" | "bearish"
    bos_level:       float           = 0.0      # broke_level from StructureBreak
    bos_close:       float           = 0.0      # close_price at BOS candle
    armed_at:        float           = 0.0      # time.time() when ARMED
    bar_count:       int             = 0        # cycles elapsed since ARMED
    retest_zone_hi:  float           = 0.0      # upper edge of FVG/OB zone
    retest_zone_lo:  float           = 0.0      # lower edge of FVG/OB zone
    zone_source:     str             = ""       # "FVG" | "OB" | "none"
    zone_tf:         str             = "1h"     # Q-5: which TF the zone came from
    # FIX: memory of already-triggered BOS levels — prevents re-arming on the
    # same level after TRIGGERED resets state to IDLE.  Stored as a set of
    # broke_level floats; cleared when a meaningfully different BOS arrives
    # (>0.5% away from every stored level).
    triggered_levels: "set[float]"  = field(default_factory=set)


# ---------------------------------------------------------------------------
# Main watcher class
# ---------------------------------------------------------------------------

class BOSRetestWatcher:
    """Per-symbol BOS retest state machine.

    Instantiate once in ``main()`` and call ``update()`` on every analysis
    cycle for each symbol before the LLM gate.
    """

    def __init__(self) -> None:
        self._states: dict[str, _SymbolState] = {}

    def _get(self, symbol: str) -> _SymbolState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolState()
        return self._states[symbol]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        symbol: str,
        primary_r: SMCAnalysisResult,
        current_price: float,
        higher_tf_r: Optional[SMCAnalysisResult] = None,
    ) -> tuple[bool, str]:
        """Evaluate BOS retest state for one symbol.

        Args:
            symbol: Asset ticker, e.g. ``'BTC/USDT'``.
            primary_r: SMCAnalysisResult for the primary timeframe (e.g. 1h).
            current_price: Current market price.
            higher_tf_r: Q-5 — Optional SMCAnalysisResult for a higher TF
                (e.g. 4h).  When provided, ``_find_retest_zone`` checks the
                higher TF FVGs first.  A higher-TF FVG zone is a stronger
                confluence and takes priority over the primary TF zone.

        Returns:
            (allow_llm: bool, reason: str)

            ``allow_llm=True``  → proceed to LLM call as normal.
            ``allow_llm=False`` → suppress LLM this cycle; reason explains why.
        """
        if not BOS_RETEST_ENABLED:
            return True, "bos_retest disabled"

        s = self._get(symbol)

        # ---- 1. Check for a fresh BOS / ChOS on the primary TF ----------
        fresh_bos = self._detect_fresh_bos(primary_r, s)

        if fresh_bos:
            bos_event, broke_level, close_price = fresh_bos
            direction = (
                "bullish" if bos_event in (StructureEvent.BOS_BULL, StructureEvent.CHOS_BULL)
                else "bearish"
            )

            # m-2: If ARMED in the opposite direction, the structure has flipped.
            # Reset to IDLE so we don't fire a stale bullish signal into a
            # now-bearish market (or vice versa).
            if s.state == RetestState.ARMED and s.bos_direction != direction:
                logger.info(
                    "[BOS_RETEST] %s structure flip — was ARMED %s, new BOS is %s — resetting",
                    symbol, s.bos_direction, direction,
                )
                s.state = RetestState.IDLE
            # Q-5: prefer higher-TF zone when available — stronger confluence
            zone, zone_tf = self._find_retest_zone(
                primary_r, direction, close_price, higher_tf_r=higher_tf_r
            )

            if zone is None:
                # No FVG/OB near the impulse — fall back to legacy immediate fire
                logger.debug(
                    "[BOS_RETEST] %s BOS %s detected but no FVG/OB zone found — immediate fire",
                    symbol, bos_event.value,
                )
                s.state = RetestState.IDLE
                return True, f"bos detected, no retest zone — immediate fire"

            s.state           = RetestState.ARMED
            s.bos_direction   = direction
            s.bos_level       = broke_level
            s.bos_close       = close_price
            s.armed_at        = time.time()
            s.bar_count       = 0
            s.retest_zone_hi  = zone[1]
            s.retest_zone_lo  = zone[0]
            s.zone_source     = zone[2]
            s.zone_tf         = zone_tf   # Q-5: track which TF the zone came from

            logger.info(
                "[BOS_RETEST] %s ARMED — %s %s | zone=%s %.4f–%.4f (tf=%s)",
                symbol, bos_event.value, direction,
                s.zone_source, s.retest_zone_lo, s.retest_zone_hi, s.zone_tf,
            )
            return False, f"BOS detected — waiting for retest of {s.zone_source} zone ({s.zone_tf})"

        # ---- 2. Handle IDLE — no pending BOS ----------------------------
        if s.state == RetestState.IDLE:
            return True, "idle — no pending BOS"

        # ---- 3. Handle ARMED — check TTL and retest ---------------------
        if s.state == RetestState.ARMED:
            s.bar_count += 1

            # TTL expiry — reset to IDLE, allow LLM to fire
            if s.bar_count > BOS_RETEST_TTL_BARS:
                logger.info(
                    "[BOS_RETEST] %s ARMED expired after %d bars — reverting to IDLE",
                    symbol, s.bar_count,
                )
                s.state = RetestState.IDLE
                return True, f"retest window expired ({s.bar_count} bars)"

            # Check if price has tapped the retest zone
            price_in_zone = self._price_in_zone(current_price, s)
            if not price_in_zone:
                return False, (
                    f"armed — waiting for retest of {s.zone_source} "
                    f"{s.retest_zone_lo:.4f}–{s.retest_zone_hi:.4f} "
                    f"(bar {s.bar_count}/{BOS_RETEST_TTL_BARS})"
                )

            # Zone tapped — check for reversal candle if required
            if BOS_RETEST_REQUIRE_REVERSAL_CANDLE:
                has_reversal = self._has_reversal_candle(primary_r, s.bos_direction)
                if not has_reversal:
                    return False, (
                        f"zone tapped but no reversal candle yet "
                        f"(bar {s.bar_count}/{BOS_RETEST_TTL_BARS})"
                    )

            # Retest confirmed — record this level so we never re-arm on it,
            # then reset to IDLE and allow the LLM to fire this cycle.
            logger.info(
                "[BOS_RETEST] %s TRIGGERED — price %.4f tapped %s zone | bars_waited=%d",
                symbol, current_price, s.zone_source, s.bar_count,
            )
            s.triggered_levels.add(s.bos_level)  # FIX: remember this level
            s.state = RetestState.IDLE  # reset immediately after trigger
            return True, f"retest triggered on {s.zone_source} zone after {s.bar_count} bars"

        # ---- 4. TRIGGERED state (should not persist — reset above) ------
        s.state = RetestState.IDLE
        return True, "triggered — reset to idle"

    def reset(self, symbol: str) -> None:
        """Force-reset a symbol to IDLE. Call on signal delivery to prevent re-fire."""
        if symbol in self._states:
            self._states[symbol] = _SymbolState()

    def state_summary(self) -> dict[str, str]:
        """Return current state for all tracked symbols — useful for health logging."""
        return {sym: s.state.value for sym, s in self._states.items()}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _detect_fresh_bos(
        self,
        primary_r: SMCAnalysisResult,
        s: _SymbolState,
    ) -> Optional[tuple[StructureEvent, float, float]]:
        """Return (event, broke_level, close_price) if a BOS newer than the
        current armed state is present, else None."""
        if primary_r.structure is None:
            return None
        latest = primary_r.structure.latest_break()
        if latest is None:
            return None

        # Avoid re-arming on the same BOS level we're already watching (ARMED guard)
        already_armed_on_this_level = (
            s.state == RetestState.ARMED and
            abs(latest.broke_level - s.bos_level) < s.bos_level * 0.001
        )
        if already_armed_on_this_level:
            return None

        # FIX: avoid re-arming on any level we already triggered on this session.
        # This prevents the IDLE→ARMED→TRIGGERED→IDLE→ARMED loop on the same FVG.
        # Clear stale triggered levels when a genuinely new BOS arrives (>0.5% away).
        threshold = latest.broke_level * 0.005  # 0.5%
        already_triggered = any(
            abs(latest.broke_level - lvl) < threshold
            for lvl in s.triggered_levels
        )
        if already_triggered:
            logger.debug(
                "[BOS_RETEST] %s skipping re-arm — broke_level %.4f already triggered",
                "?", latest.broke_level,
            )
            return None

        # New BOS at a meaningfully different level — clear stale triggered memory
        # so old levels don't block forever when price revisits a zone much later.
        if s.triggered_levels:
            all_stale = all(
                abs(latest.broke_level - lvl) > latest.broke_level * 0.02  # 2% away = new zone
                for lvl in s.triggered_levels
            )
            if all_stale:
                s.triggered_levels.clear()

        return (latest.event, latest.broke_level, latest.close_price)

    def _find_retest_zone(
        self,
        primary_r: SMCAnalysisResult,
        direction: str,
        impulse_close: float,
        higher_tf_r: Optional[SMCAnalysisResult] = None,
    ) -> tuple[Optional[tuple[float, float, str]], str]:
        """Find the nearest FVG or OB left by the BOS impulse candle.

        Q-5: When ``higher_tf_r`` is supplied, checks the higher TF FVGs first.
        A higher-TF FVG is a stronger confluence zone — price respects 4H FVGs
        far more reliably than 1H FVGs during retests.  Only falls through to
        the primary TF when the higher TF has no qualifying zone.

        For a bullish BOS: look for a bullish FVG or bullish OB *below* the
        impulse close (the zone price should retrace into).
        For a bearish BOS: look for a bearish FVG or bearish OB *above* the
        impulse close.

        Returns ((zone_low, zone_high, source_label), timeframe_str) or (None, "").
        """
        # Q-5: check higher TF FVG first — stronger institutional zone
        if higher_tf_r is not None:
            htf_fvg = self._best_fvg(higher_tf_r, direction, impulse_close)
            if htf_fvg:
                tf_label = higher_tf_r.timeframe if higher_tf_r.timeframe else "htf"
                logger.debug(
                    "[BOS_RETEST] Using higher-TF (%s) FVG zone %.4f–%.4f",
                    tf_label, htf_fvg[0], htf_fvg[1],
                )
                return htf_fvg, tf_label

        # --- Try primary TF FVG (higher probability retest zone) ----------
        fvg_zone = self._best_fvg(primary_r, direction, impulse_close)
        if fvg_zone:
            return fvg_zone, primary_r.timeframe

        # --- Fall back to Order Block on primary TF -----------------------
        ob_zone = self._best_ob(primary_r, direction, impulse_close)
        if ob_zone:
            return ob_zone, primary_r.timeframe

        return None, ""

    def _best_fvg(
        self,
        primary_r: SMCAnalysisResult,
        direction: str,
        impulse_close: float,
    ) -> Optional[tuple[float, float, str]]:
        """Find the nearest active FVG that matches direction and is within
        BOS_RETEST_FVG_TOLERANCE of the impulse close price."""
        candidates: list[FairValueGap] = [
            fvg for fvg in primary_r.active_fvgs
            if fvg.direction == direction
        ]
        if not candidates:
            return None

        tol = BOS_RETEST_FVG_TOLERANCE
        valid = []
        for fvg in candidates:
            mid = (fvg.top + fvg.bottom) / 2
            dist_pct = abs(mid - impulse_close) / impulse_close
            if dist_pct <= tol:
                valid.append((dist_pct, fvg))

        if not valid:
            return None

        # Closest FVG wins
        _, best = min(valid, key=lambda x: x[0])
        return (best.bottom, best.top, "FVG")

    def _best_ob(
        self,
        primary_r: SMCAnalysisResult,
        direction: str,
        impulse_close: float,
    ) -> Optional[tuple[float, float, str]]:
        """Find the nearest active Order Block that matches direction and is
        within BOS_RETEST_OB_TOLERANCE of the impulse close price."""
        ob_dir = "bullish" if direction == "bullish" else "bearish"
        candidates: list[OrderBlock] = [
            ob for ob in primary_r.active_obs
            if ob.direction == ob_dir
        ]
        if not candidates:
            return None

        tol = BOS_RETEST_OB_TOLERANCE
        valid = []
        for ob in candidates:
            mid = (ob.top + ob.bottom) / 2
            dist_pct = abs(mid - impulse_close) / impulse_close
            if dist_pct <= tol:
                valid.append((dist_pct, ob))

        if not valid:
            return None

        _, best = min(valid, key=lambda x: x[0])
        return (best.bottom, best.top, "OB")

    def _price_in_zone(self, price: float, s: _SymbolState) -> bool:
        """Return True if current price has entered the retest zone."""
        tol_pct = (
            BOS_RETEST_FVG_TOLERANCE if s.zone_source == "FVG"
            else BOS_RETEST_OB_TOLERANCE
        )
        # Allow a small tolerance buffer below/above zone edges
        buffer = s.retest_zone_lo * tol_pct * 0.5
        return (s.retest_zone_lo - buffer) <= price <= (s.retest_zone_hi + buffer)

    def _has_reversal_candle(
        self,
        primary_r: SMCAnalysisResult,
        bos_direction: str,
    ) -> bool:
        """Check if the most recent candlestick patterns include a reversal
        aligned with the BOS direction (continuation after retest)."""
        if not primary_r.candlestick_patterns:
            return False

        # Reversal candles that confirm bullish continuation after retest
        BULL_REVERSAL = {
            "hammer", "inverted_hammer", "bullish_engulfing",
            "morning_star", "piercing_line", "tweezer_bottom",
            "three_white_soldiers",
        }
        # Reversal candles that confirm bearish continuation after retest
        BEAR_REVERSAL = {
            "shooting_star", "hanging_man", "bearish_engulfing",
            "evening_star", "dark_cloud_cover", "tweezer_top",
            "three_black_crows",
        }

        expected = BULL_REVERSAL if bos_direction == "bullish" else BEAR_REVERSAL
        # F-C2: was [:3] (first 3 — oldest patterns), must be [-3:] (last 3 — most recent)
        recent = primary_r.candlestick_patterns[-3:]
        return any(p.pattern.lower() in expected for p in recent)
