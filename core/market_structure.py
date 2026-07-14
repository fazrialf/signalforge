"""
SignalForge — Market Structure Tracker
Tracks HH/HL/LH/LL sequence and detects BOS and ChOS.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from core.swing_points import SwingPoint, detect_swing_points
from config.settings import IMPULSE_ATR_MULT

logger = logging.getLogger(__name__)


class Bias(str, Enum):
    BULLISH  = "bullish"
    BEARISH  = "bearish"
    RANGING  = "ranging"
    UNKNOWN  = "unknown"


class StructureEvent(str, Enum):
    BOS_BULL  = "BOS_BULL"   # Break of Structure — bullish continuation
    BOS_BEAR  = "BOS_BEAR"   # Break of Structure — bearish continuation
    CHOS_BULL = "CHOS_BULL"  # Change of Structure — bullish reversal
    CHOS_BEAR = "CHOS_BEAR"  # Change of Structure — bearish reversal


@dataclass
class StructureBreak:
    event:     StructureEvent
    bar_index: int
    timestamp: pd.Timestamp
    broke_level: float   # the swing high/low that was broken
    close_price: float


@dataclass
class MarketStructure:
    """Full market structure analysis result."""
    bias:         Bias
    last_hh:      Optional[SwingPoint]
    last_hl:      Optional[SwingPoint]
    last_lh:      Optional[SwingPoint]
    last_ll:      Optional[SwingPoint]
    structure_breaks: list[StructureBreak]
    swing_points: list[SwingPoint]

    def latest_break(self) -> Optional[StructureBreak]:
        return self.structure_breaks[-1] if self.structure_breaks else None

    def to_summary(self) -> str:
        """Human-readable summary for LLM prompts."""
        lines = [f"Bias: {self.bias.value.upper()}"]
        if self.last_hh:
            lines.append(f"Last HH: ${self.last_hh.price:,.2f} @ {self.last_hh.timestamp}")
        if self.last_hl:
            lines.append(f"Last HL: ${self.last_hl.price:,.2f} @ {self.last_hl.timestamp}")
        if self.last_lh:
            lines.append(f"Last LH: ${self.last_lh.price:,.2f} @ {self.last_lh.timestamp}")
        if self.last_ll:
            lines.append(f"Last LL: ${self.last_ll.price:,.2f} @ {self.last_ll.timestamp}")
        sb = self.latest_break()
        if sb:
            lines.append(f"Last structure break: {sb.event.value} @ ${sb.broke_level:,.2f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "bias": self.bias.value,
            "last_hh": self.last_hh.price if self.last_hh else None,
            "last_hl": self.last_hl.price if self.last_hl else None,
            "last_lh": self.last_lh.price if self.last_lh else None,
            "last_ll": self.last_ll.price if self.last_ll else None,
            "last_break": self.latest_break().event.value if self.latest_break() else None,
            "last_break_level": self.latest_break().broke_level if self.latest_break() else None,
        }


def analyse_structure(df: pd.DataFrame,
                      lookback: int = 5) -> MarketStructure:
    """
    Full market structure analysis:
    1. Detect swing points
    2. Label HH / HL / LH / LL
    3. Detect BOS and ChOS
    4. Determine overall bias
    """
    swings = detect_swing_points(df, lookback=lookback)
    if len(swings) < 4:
        return MarketStructure(
            bias=Bias.UNKNOWN,
            last_hh=None, last_hl=None, last_lh=None, last_ll=None,
            structure_breaks=[], swing_points=swings
        )

    closes = df["close"].to_numpy(dtype=float)
    highs  = df["high"].to_numpy(dtype=float)
    lows   = df["low"].to_numpy(dtype=float)

    # --- ATR for impulse/displacement filter ---
    # A BOS must be accompanied by displacement ≥ IMPULSE_ATR_MULT × ATR
    # to filter shallow, low-energy breaks that don't represent real structure.
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1]),
        )
    )
    atr14 = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr)) if len(tr) > 0 else 0.0
    impulse_min = atr14 * IMPULSE_ATR_MULT

    # --- Separate highs and lows ---
    s_highs = [p for p in swings if p.type == "high"]
    s_lows  = [p for p in swings if p.type == "low"]

    # --- Detect BOS / ChOS (single left-to-right pass — no look-ahead bias) ---
    # Labels are initialised to None and updated only as we advance through
    # swings. Pre-scanning the full array would stamp future swing labels onto
    # past bars, making back-of-dataset breaks use information that wasn't yet
    # available — invalid in live trading.
    last_hh: Optional[SwingPoint] = None
    last_hl: Optional[SwingPoint] = None
    last_lh: Optional[SwingPoint] = None
    last_ll: Optional[SwingPoint] = None

    breaks: list[StructureBreak] = []
    current_bias = Bias.UNKNOWN

    for i in range(4, len(swings)):
        p = swings[i]
        bar_close = closes[p.index] if p.index < len(closes) else p.price

        if current_bias in (Bias.BULLISH, Bias.UNKNOWN):
            # Bullish trend: watch for bearish BOS (break below last HL)
            if last_hl and p.type == "low" and bar_close < last_hl.price:
                displacement = abs(last_hl.price - bar_close)
                if displacement >= impulse_min:
                    event = StructureEvent.CHOS_BEAR if current_bias == Bias.BULLISH else StructureEvent.BOS_BEAR
                    breaks.append(StructureBreak(
                        event=event, bar_index=p.index,
                        timestamp=p.timestamp,
                        broke_level=last_hl.price,
                        close_price=float(bar_close)
                    ))
                    current_bias = Bias.BEARISH

        if current_bias in (Bias.BEARISH, Bias.UNKNOWN):
            # Bearish trend: watch for bullish BOS (break above last LH)
            if last_lh and p.type == "high" and bar_close > last_lh.price:
                displacement = abs(bar_close - last_lh.price)
                if displacement >= impulse_min:
                    event = StructureEvent.CHOS_BULL if current_bias == Bias.BEARISH else StructureEvent.BOS_BULL
                    breaks.append(StructureBreak(
                        event=event, bar_index=p.index,
                        timestamp=p.timestamp,
                        broke_level=last_lh.price,
                        close_price=float(bar_close)
                    ))
                    current_bias = Bias.BULLISH

        # Update last swing labels on the fly
        if p.type == "high":
            prev_h = next((x for x in reversed(swings[:i]) if x.type == "high"), None)
            if prev_h:
                if p.price > prev_h.price:
                    last_hh = p
                else:
                    last_lh = p
        else:
            prev_l = next((x for x in reversed(swings[:i]) if x.type == "low"), None)
            if prev_l:
                if p.price > prev_l.price:
                    last_hl = p
                else:
                    last_ll = p

    # --- Final bias determination ---
    if current_bias == Bias.UNKNOWN:
        # Fallback: compare last 2 highs and lows
        if len(s_highs) >= 2 and len(s_lows) >= 2:
            hh_trend = s_highs[-1].price > s_highs[-2].price
            hl_trend = s_lows[-1].price  > s_lows[-2].price
            if hh_trend and hl_trend:
                current_bias = Bias.BULLISH
            elif not hh_trend and not hl_trend:
                current_bias = Bias.BEARISH
            else:
                current_bias = Bias.RANGING

    return MarketStructure(
        bias=current_bias,
        last_hh=last_hh, last_hl=last_hl,
        last_lh=last_lh, last_ll=last_ll,
        structure_breaks=breaks,
        swing_points=swings
    )


if __name__ == "__main__":
    import asyncio, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from db.schema import init_db
    from data.fetcher import DataFetcher

    async def test():
        init_db()
        fetcher = DataFetcher("BTC/USDT", ["4h"], history_bars=200)
        data = await fetcher.load_all()
        await fetcher.close()
        ms = analyse_structure(data["4h"], lookback=5)
        print("\nMarket Structure (4H BTC/USDT):")
        print(ms.to_summary())
        print(f"\nStructure breaks detected: {len(ms.structure_breaks)}")
        for b in ms.structure_breaks[-5:]:
            print(f"  {b.event.value:12s}  broke ${b.broke_level:>10,.2f}  @ {b.timestamp}")

    asyncio.run(test())
