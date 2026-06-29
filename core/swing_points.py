"""
SignalForge — Swing Point Detector
Identifies swing highs and swing lows using N-bar lookback.
Foundation for all structural analysis (BOS, ChOS, S/R, liquidity).
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SwingPoint:
    """A detected swing high or swing low."""
    index: int              # bar index in the DataFrame
    timestamp: pd.Timestamp
    price: float
    type: str               # 'high' or 'low'
    strength: int = 1       # how many bars on each side confirmed it


def detect_swing_points(df: pd.DataFrame,
                        lookback: int = 5) -> list[SwingPoint]:
    """
    Detect swing highs and swing lows.

    A swing HIGH at bar i requires:
      high[i] >= max(high[i-lookback : i]) AND
      high[i] >= max(high[i+1 : i+lookback+1])

    A swing LOW at bar i requires:
      low[i] <= min(low[i-lookback : i]) AND
      low[i] <= min(low[i+1 : i+lookback+1])

    Returns list of SwingPoint ordered by bar index.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    points: list[SwingPoint] = []

    for i in range(lookback, n - lookback):
        # --- Swing High ---
        left_max = np.max(highs[i - lookback:i])
        right_max = np.max(highs[i + 1:i + lookback + 1])
        if highs[i] >= left_max and highs[i] >= right_max:
            # Calculate strength: how many bars on each side is it the highest?
            strength = lookback
            for s in range(lookback + 1, min(i, n - i)):
                if i - s >= 0 and i + s < n:
                    if highs[i] >= highs[i - s] and highs[i] >= highs[i + s]:
                        strength = s
                    else:
                        break
                else:
                    break
            points.append(SwingPoint(
                index=i,
                timestamp=df.index[i],
                price=float(highs[i]),
                type="high",
                strength=strength,
            ))

        # --- Swing Low ---
        left_min = np.min(lows[i - lookback:i])
        right_min = np.min(lows[i + 1:i + lookback + 1])
        if lows[i] <= left_min and lows[i] <= right_min:
            strength = lookback
            for s in range(lookback + 1, min(i, n - i)):
                if i - s >= 0 and i + s < n:
                    if lows[i] <= lows[i - s] and lows[i] <= lows[i + s]:
                        strength = s
                    else:
                        break
                else:
                    break
            points.append(SwingPoint(
                index=i,
                timestamp=df.index[i],
                price=float(lows[i]),
                type="low",
                strength=strength,
            ))

    # Sort by index (time order)
    points.sort(key=lambda p: p.index)

    # Remove duplicates at the same bar (can't be both H and L in practice,
    # but keep both if they occur — rare edge case in doji-like bars)
    return points


def get_recent_swings(points: list[SwingPoint],
                      n: int = 10) -> list[SwingPoint]:
    """Return the N most recent swing points."""
    return points[-n:] if len(points) > n else points


def swing_points_to_dict(points: list[SwingPoint]) -> list[dict]:
    """Convert swing points to a list of dicts (for JSON serialisation)."""
    return [
        {
            "index": p.index,
            "timestamp": str(p.timestamp),
            "price": p.price,
            "type": p.type,
            "strength": p.strength,
        }
        for p in points
    ]


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

        df = data["4h"]
        points = detect_swing_points(df, lookback=5)
        print(f"\nDetected {len(points)} swing points on 4H BTC/USDT:")
        for p in points[-15:]:
            print(f"  {p.type:5s}  ${p.price:>10,.2f}  strength={p.strength}  {p.timestamp}")

    asyncio.run(test())
