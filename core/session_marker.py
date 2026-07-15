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
