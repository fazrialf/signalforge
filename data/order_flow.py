"""data/order_flow.py — Cumulative Delta (Order Flow) from aggTrades

Tracks buy vs sell volume to compute cumulative delta — the running sum
of (buy_volume - sell_volume). Used by the Delta Divergence strategy to
detect when price moves on weak flow (reversal signal).

Data source: Binance aggTrades WebSocket stream.
A trade is classified as a "buy" when the taker is the buyer (is_buyer_maker=False).

Key metrics:
- Cumulative Delta: running Σ(buy_vol - sell_vol) per bar
- Delta per bar: net buy/sell pressure on each 5m candle
- Delta divergence: price makes new high but delta makes lower high (or vice versa)
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class DeltaBar:
    """Aggregated delta for one candle bar.

    Attributes:
        timestamp: Bar open time (unix ms).
        buy_volume: Total taker-buy volume in this bar.
        sell_volume: Total taker-sell volume in this bar.
        delta: buy_volume - sell_volume (positive = net buying).
        cum_delta: Running cumulative delta up to this bar.
        trades_count: Number of individual trades aggregated.
    """
    timestamp: int
    buy_volume: float
    sell_volume: float
    delta: float
    cum_delta: float
    trades_count: int


@dataclass
class DeltaState:
    """Current order flow state for one asset.

    Attributes:
        current_delta: Net delta of the current (forming) bar.
        cum_delta: Running cumulative delta.
        bars: Recent completed DeltaBars (last N bars).
        delta_divergence: Whether delta diverges from price.
        divergence_type: 'bearish' (price up, delta down) or 'bullish' or ''.
        delta_trend: 'rising', 'falling', or 'flat' (over last 5 bars).
    """
    current_delta: float
    cum_delta: float
    bars: list[DeltaBar] = field(default_factory=list)
    delta_divergence: bool = False
    divergence_type: str = ""
    delta_trend: str = "flat"

    def to_dict(self) -> dict:
        return {
            "current_delta": round(self.current_delta, 4),
            "cum_delta": round(self.cum_delta, 4),
            "recent_bars": len(self.bars),
            "delta_divergence": self.delta_divergence,
            "divergence_type": self.divergence_type,
            "delta_trend": self.delta_trend,
        }


# ---------------------------------------------------------------------------
# Order Flow Accumulator
# ---------------------------------------------------------------------------

class OrderFlowAccumulator:
    """Accumulates aggTrade data into per-bar delta metrics.

    Usage:
        accumulator = OrderFlowAccumulator(bar_interval_ms=300_000)  # 5m bars
        # On each aggTrade from WebSocket:
        accumulator.on_trade(symbol, price, qty, is_buyer_maker, timestamp_ms)
        # On each analysis cycle:
        state = accumulator.get_state(symbol)
    """

    def __init__(self, bar_interval_ms: int = 300_000, max_bars: int = 100):
        """
        Args:
            bar_interval_ms: Bar width in milliseconds (300_000 = 5 minutes).
            max_bars: Maximum completed bars to retain per symbol.
        """
        self._bar_interval_ms = bar_interval_ms
        self._max_bars = max_bars

        # Per-symbol state
        self._current_bar_start: dict[str, int] = {}
        self._current_buy_vol: dict[str, float] = defaultdict(float)
        self._current_sell_vol: dict[str, float] = defaultdict(float)
        self._current_trade_count: dict[str, int] = defaultdict(int)
        self._completed_bars: dict[str, list[DeltaBar]] = defaultdict(list)
        self._cum_delta: dict[str, float] = defaultdict(float)

    def on_trade(
        self,
        symbol: str,
        price: float,
        qty: float,
        is_buyer_maker: bool,
        timestamp_ms: int,
    ) -> None:
        """Process a single aggTrade.

        Args:
            symbol: Asset ticker e.g. 'BTC/USDT'.
            price: Trade price.
            qty: Trade quantity.
            is_buyer_maker: If True, the maker is the buyer → taker is selling.
                If False, the taker is buying (aggressive buy).
            timestamp_ms: Trade timestamp in milliseconds.
        """
        # Determine bar boundaries
        bar_start = (timestamp_ms // self._bar_interval_ms) * self._bar_interval_ms

        # Check if we've moved to a new bar
        prev_bar_start = self._current_bar_start.get(symbol, 0)
        if bar_start > prev_bar_start and prev_bar_start > 0:
            # Complete the previous bar
            self._close_bar(symbol, prev_bar_start)

        self._current_bar_start[symbol] = bar_start

        # Accumulate volume
        volume = price * qty  # notional volume in quote currency
        if is_buyer_maker:
            # Taker is selling (aggressive sell)
            self._current_sell_vol[symbol] += volume
        else:
            # Taker is buying (aggressive buy)
            self._current_buy_vol[symbol] += volume

        self._current_trade_count[symbol] += 1

    def _close_bar(self, symbol: str, bar_start_ms: int) -> None:
        """Close the current bar and add it to completed bars."""
        buy_vol = self._current_buy_vol[symbol]
        sell_vol = self._current_sell_vol[symbol]
        delta = buy_vol - sell_vol
        self._cum_delta[symbol] += delta

        bar = DeltaBar(
            timestamp=bar_start_ms,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            delta=delta,
            cum_delta=self._cum_delta[symbol],
            trades_count=self._current_trade_count[symbol],
        )

        bars = self._completed_bars[symbol]
        bars.append(bar)

        # Trim to max_bars
        if len(bars) > self._max_bars:
            self._completed_bars[symbol] = bars[-self._max_bars:]

        # Reset current bar accumulators
        self._current_buy_vol[symbol] = 0.0
        self._current_sell_vol[symbol] = 0.0
        self._current_trade_count[symbol] = 0

    def get_live_snapshot(self, symbol: str) -> dict:
        """Lightweight live metrics for health logging (no min-bar requirement)."""
        bars = self._completed_bars.get(symbol, [])
        return {
            "completed_bars": len(bars),
            "current_trades": int(self._current_trade_count.get(symbol, 0)),
            "current_delta": float(
                self._current_buy_vol.get(symbol, 0.0)
                - self._current_sell_vol.get(symbol, 0.0)
            ),
            "cum_delta": float(self._cum_delta.get(symbol, 0.0)),
            "current_buy_vol": float(self._current_buy_vol.get(symbol, 0.0)),
            "current_sell_vol": float(self._current_sell_vol.get(symbol, 0.0)),
        }

    def get_state(self, symbol: str) -> Optional[DeltaState]:
        """Get the current delta state for a symbol.

        Returns None if no trade data has been accumulated yet.
        Partial state (0 completed bars) is returned while the first bar forms.
        """
        bars = self._completed_bars.get(symbol, [])
        # Still expose a partial state when we have live accumulation but
        # fewer than 3 completed bars — strategy can ignore it, health logs need it.
        if len(bars) < 1 and self._current_trade_count.get(symbol, 0) == 0:
            return None

        current_delta = (
            self._current_buy_vol[symbol] - self._current_sell_vol[symbol]
        )
        cum_delta = self._cum_delta.get(symbol, 0.0)

        # Compute delta trend over last 5 bars (needs ≥3 completed)
        if len(bars) >= 3:
            delta_trend = _compute_delta_trend(bars[-5:])
            divergence, div_type = _detect_delta_divergence(bars[-10:])
        else:
            delta_trend = "flat"
            divergence, div_type = False, ""

        return DeltaState(
            current_delta=current_delta,
            cum_delta=cum_delta,
            bars=bars[-20:],  # expose last 20 bars
            delta_divergence=divergence,
            divergence_type=div_type,
            delta_trend=delta_trend,
        )

    def get_recent_bars(self, symbol: str, n: int = 20) -> list[DeltaBar]:
        """Get the last N completed delta bars for a symbol."""
        bars = self._completed_bars.get(symbol, [])
        return bars[-n:]

    def reset(self, symbol: str) -> None:
        """Reset all state for a symbol."""
        self._current_bar_start.pop(symbol, None)
        self._current_buy_vol.pop(symbol, None)
        self._current_sell_vol.pop(symbol, None)
        self._current_trade_count.pop(symbol, None)
        self._completed_bars.pop(symbol, None)
        self._cum_delta.pop(symbol, None)


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _compute_delta_trend(bars: list[DeltaBar]) -> str:
    """Determine if cumulative delta is rising, falling, or flat."""
    if len(bars) < 3:
        return "flat"

    deltas = [b.cum_delta for b in bars]
    # Simple linear regression slope
    x = np.arange(len(deltas), dtype=float)
    y = np.array(deltas, dtype=float)

    if y.std() == 0:
        return "flat"

    # Normalise slope by the range to get a relative measure
    slope = np.polyfit(x, y, 1)[0]
    normalised = slope / (abs(y.mean()) + 1e-10)

    if normalised > 0.01:
        return "rising"
    elif normalised < -0.01:
        return "falling"
    return "flat"


def _detect_delta_divergence(bars: list[DeltaBar]) -> tuple[bool, str]:
    """Detect divergence between cumulative delta direction and bar deltas.

    Bearish divergence: cum_delta making higher highs but recent bars have
    negative delta (buying pressure fading despite new highs).

    Bullish divergence: cum_delta making lower lows but recent bars have
    positive delta (selling pressure fading despite new lows).
    """
    if len(bars) < 5:
        return False, ""

    # Split into first half and second half
    mid = len(bars) // 2
    first_half = bars[:mid]
    second_half = bars[mid:]

    first_cum_high = max(b.cum_delta for b in first_half)
    second_cum_high = max(b.cum_delta for b in second_half)
    first_cum_low = min(b.cum_delta for b in first_half)
    second_cum_low = min(b.cum_delta for b in second_half)

    # Recent bar deltas (last 3)
    recent_deltas = [b.delta for b in bars[-3:]]
    avg_recent_delta = sum(recent_deltas) / len(recent_deltas)

    # Bearish: cum_delta still high/rising but recent deltas are negative
    # (buyers exhausted — price will reverse down)
    if second_cum_high >= first_cum_high and avg_recent_delta < 0:
        return True, "bearish"

    # Bullish: cum_delta still low/falling but recent deltas are positive
    # (sellers exhausted — price will reverse up)
    if second_cum_low <= first_cum_low and avg_recent_delta > 0:
        return True, "bullish"

    return False, ""


def detect_delta_price_divergence(
    delta_bars: list[DeltaBar],
    price_highs: list[float],
    price_lows: list[float],
    lookback: int = 10,
) -> tuple[bool, str]:
    """Detect divergence between price and cumulative delta.

    This is the stronger signal: price makes a new swing high but delta
    makes a lower high (or price makes new low but delta makes higher low).

    Args:
        delta_bars: Recent DeltaBars.
        price_highs: Recent price highs (same length/alignment as delta_bars).
        price_lows: Recent price lows.
        lookback: Number of bars to compare.

    Returns:
        (has_divergence, type) where type is 'bearish' or 'bullish' or ''.
    """
    n = min(lookback, len(delta_bars), len(price_highs), len(price_lows))
    if n < 5:
        return False, ""

    # Use last N bars
    bars = delta_bars[-n:]
    highs = price_highs[-n:]
    lows = price_lows[-n:]

    mid = n // 2
    first_half_highs = highs[:mid]
    second_half_highs = highs[mid:]
    first_half_lows = lows[:mid]
    second_half_lows = lows[mid:]

    first_cum_highs = [b.cum_delta for b in bars[:mid]]
    second_cum_highs = [b.cum_delta for b in bars[mid:]]

    # Bearish divergence: price higher high + delta lower high
    price_higher_high = max(second_half_highs) > max(first_half_highs)
    delta_lower_high = max(second_cum_highs) < max(first_cum_highs)
    if price_higher_high and delta_lower_high:
        return True, "bearish"

    # Bullish divergence: price lower low + delta higher low
    price_lower_low = min(second_half_lows) < min(first_half_lows)
    delta_higher_low = min(second_cum_highs) > min(first_cum_highs)
    if price_lower_low and delta_higher_low:
        return True, "bullish"

    return False, ""
