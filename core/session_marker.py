"""
Session Marker — detects current forex/crypto trading session and fires
one-shot Telegram alerts at each session open.

Sessions (UTC):
  Asia      00:00 – 08:00   🌏
  London    07:00 – 16:00   🇬🇧   (overlaps Asia 07:00–08:00)
  Overlap   12:00 – 16:00   🔁   (London + New York overlap — highest volatility)
  New York  12:00 – 21:00   🇺🇸
  Off-hours 21:00 – 00:00   😴
"""

from __future__ import annotations
import datetime
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Session definitions
# Each entry: (name, emoji, open_utc_hour, close_utc_hour, description)
# Order matters — more specific sessions (Overlap) checked before broader ones.
# ---------------------------------------------------------------------------
@dataclass
class Session:
    name: str
    emoji: str
    open_hour: int    # inclusive
    close_hour: int   # exclusive
    description: str
    high_volatility: bool = False


SESSIONS: list[Session] = [
    Session("London/NY Overlap", "🔁", 12, 16,
            "Highest liquidity window — London + New York both active",
            high_volatility=True),
    Session("New York",          "🇺🇸", 12, 21,
            "NY session — US equities + crypto correlation elevated"),
    Session("London",            "🇬🇧",  7, 16,
            "London session — peak forex liquidity, GBP/EUR pairs active"),
    Session("Asia (Tokyo)",      "🌏",   0,  8,
            "Asian session — lower liquidity, JPY pairs active"),
    Session("Off-hours",         "😴",  21, 24,
            "Low liquidity — wider spreads, higher false sweep risk"),
]


def get_current_session(utc_now: Optional[datetime.datetime] = None) -> Session:
    """Return the most specific active session for the given UTC time."""
    if utc_now is None:
        utc_now = datetime.datetime.now(datetime.timezone.utc)
    hour = utc_now.hour

    for session in SESSIONS:
        if session.open_hour <= hour < session.close_hour:
            return session

    # Fallback — should never happen but covers edge cases
    return Session("Off-hours", "😴", 21, 24,
                   "Low liquidity — wider spreads, higher false sweep risk")


def get_session_opens_in_window(
    last_checked_utc: datetime.datetime,
    now_utc: Optional[datetime.datetime] = None,
) -> list[Session]:
    """
    Return sessions whose open hour falls within (last_checked_utc, now_utc].
    Use this to fire one-shot alerts: call every minute, fire when non-empty.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

    fired: list[Session] = []
    # Walk through each minute between last_checked and now
    # For typical 1-min polling, this is at most 1–2 iterations
    cursor = last_checked_utc.replace(second=0, microsecond=0)
    end    = now_utc.replace(second=0, microsecond=0)

    seen: set[str] = set()
    while cursor <= end:
        h = cursor.hour
        for session in SESSIONS:
            if session.name == "Off-hours":
                continue  # no alert for off-hours open
            if session.open_hour == h and session.name not in seen:
                seen.add(session.name)
                fired.append(session)
        cursor += datetime.timedelta(minutes=1)

    return fired


def format_session_label(session: Session) -> str:
    """Short label for embedding in Structure Snapshot messages."""
    vol = " ⚡HIGH VOL" if session.high_volatility else ""
    return f"{session.emoji} {session.name}{vol}"


def format_session_open_alert(session: Session, utc_now: datetime.datetime) -> str:
    """Full Telegram message for a session open alert."""
    time_str = utc_now.strftime("%H:%M UTC")
    lines = [
        f"{session.emoji} <b>{session.name} Session Open</b> — {time_str}",
        f"{session.description}",
    ]
    if session.high_volatility:
        lines.append("⚡ <b>High volatility window</b> — expect increased spread and sharp moves.")
    if session.name == "London":
        lines.append("👀 Watch for liquidity sweeps in the first 30 min before trend continuation.")
    if session.name == "New York":
        lines.append("👀 NY open often triggers BOS on majors — confirm structure before entry.")
    if session.name == "London/NY Overlap":
        lines.append("🎯 Best signal window of the day — both institutional desks active.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Asia Session Range Tracker
# ---------------------------------------------------------------------------

@dataclass
class AsiaRange:
    """The high/low range formed during the Asia session (00:00–07:00 UTC).

    Used by the Session Breakout strategy: at London open, a sweep of one
    end of this range followed by a reclaim is a high-probability entry.

    Attributes:
        high: Highest price during Asia session.
        low: Lowest price during Asia session.
        midpoint: (high + low) / 2 — equilibrium.
        range_pct: (high - low) / midpoint × 100 — range width as %.
        bars_counted: How many bars formed the range.
        complete: Whether the Asia session has ended (07:00+ UTC).
        swept_high: Whether price has gone above the high after session end.
        swept_low: Whether price has gone below the low after session end.
    """
    high: float
    low: float
    midpoint: float
    range_pct: float
    bars_counted: int
    complete: bool
    swept_high: bool = False
    swept_low: bool = False

    def to_dict(self) -> dict:
        return {
            "high": round(self.high, 6),
            "low": round(self.low, 6),
            "midpoint": round(self.midpoint, 6),
            "range_pct": round(self.range_pct, 3),
            "bars_counted": self.bars_counted,
            "complete": self.complete,
            "swept_high": self.swept_high,
            "swept_low": self.swept_low,
        }


# Asia session: 00:00–07:00 UTC
_ASIA_OPEN_HOUR = 0
_ASIA_CLOSE_HOUR = 7


def calc_asia_range(
    df: "pd.DataFrame",
    utc_now: Optional[datetime.datetime] = None,
) -> Optional[AsiaRange]:
    """Calculate the Asia session (00:00–07:00 UTC) high/low range.

    Works on 5m candle data. Identifies candles that fall within today's
    (or yesterday's, if before Asia open) Asia session and computes the
    range.

    Args:
        df: OHLCV DataFrame with DatetimeIndex (UTC).
        utc_now: Current UTC time. Defaults to now.

    Returns:
        AsiaRange if enough data, else None.
    """
    import pandas as pd

    if df is None or len(df) < 10:
        return None

    if utc_now is None:
        utc_now = datetime.datetime.now(datetime.timezone.utc)

    # Determine which date's Asia session to use:
    # - If current hour >= 7 (Asia ended today), use today's Asia range
    # - If current hour < 7 (still in Asia), use yesterday's completed range
    if utc_now.hour >= _ASIA_CLOSE_HOUR:
        asia_date = utc_now.date()
    else:
        asia_date = (utc_now - datetime.timedelta(days=1)).date()

    # Filter candles within the Asia session window
    try:
        idx = df.index
        if not hasattr(idx, 'hour'):
            return None

        asia_start = datetime.datetime.combine(
            asia_date, datetime.time(_ASIA_OPEN_HOUR, 0),
            tzinfo=datetime.timezone.utc,
        )
        asia_end = datetime.datetime.combine(
            asia_date, datetime.time(_ASIA_CLOSE_HOUR, 0),
            tzinfo=datetime.timezone.utc,
        )

        # Handle timezone-naive index
        if idx.tzinfo is None:
            asia_start = asia_start.replace(tzinfo=None)
            asia_end = asia_end.replace(tzinfo=None)

        asia_mask = (idx >= asia_start) & (idx < asia_end)
        asia_candles = df[asia_mask]

        if len(asia_candles) < 3:
            return None

        high = float(asia_candles["high"].max())
        low = float(asia_candles["low"].min())
        midpoint = (high + low) / 2.0
        range_pct = ((high - low) / midpoint) * 100 if midpoint > 0 else 0.0

        # Is Asia session complete?
        complete = utc_now.hour >= _ASIA_CLOSE_HOUR

        # Check if range has been swept (only relevant after session ends)
        swept_high = False
        swept_low = False
        if complete:
            post_asia = df[idx >= asia_end]
            if len(post_asia) > 0:
                post_high = float(post_asia["high"].max())
                post_low = float(post_asia["low"].min())
                swept_high = post_high > high
                swept_low = post_low < low

        return AsiaRange(
            high=high,
            low=low,
            midpoint=midpoint,
            range_pct=range_pct,
            bars_counted=len(asia_candles),
            complete=complete,
            swept_high=swept_high,
            swept_low=swept_low,
        )

    except Exception:
        return None


def is_asia_range_sweep(
    asia_range: Optional[AsiaRange],
    current_price: float,
    tolerance_pct: float = 0.001,
) -> tuple[bool, str]:
    """Check if current price is sweeping the Asia range.

    Returns:
        (is_sweeping, direction)
        direction: 'bullish' (swept lows, expect reversal up) or
                   'bearish' (swept highs, expect reversal down) or ''
    """
    if asia_range is None or not asia_range.complete:
        return False, ""

    buffer = asia_range.high * tolerance_pct

    # Sweeping above Asia high → bearish (stop hunt above, expect reversal down)
    if current_price > asia_range.high + buffer:
        return True, "bearish"

    # Sweeping below Asia low → bullish (stop hunt below, expect reversal up)
    if current_price < asia_range.low - buffer:
        return True, "bullish"

    return False, ""

