"""
external/economic_calendar.py

High-impact macro event detector for SignalForge.

Detects upcoming high-impact macro events (CPI, FOMC, NFP) to support
Filter 7: "no entry within 2H of a high-impact event".

TODO: Replace hardcoded events with TradingEconomics API or Investing.com scraper.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded event calendar — update each quarter as release dates are published.
# All times are UTC.
# ---------------------------------------------------------------------------
EVENTS_2026: list[dict] = [
    # ── June 2026 ──────────────────────────────────────────────────────────
    {"event": "US CPI",      "date": "2026-06-11 12:30", "impact": "high"},
    {"event": "FOMC Meeting", "date": "2026-06-18 18:00", "impact": "high"},
    {"event": "NFP",         "date": "2026-06-05 12:30", "impact": "high"},
    # ── July 2026 ──────────────────────────────────────────────────────────
    {"event": "NFP",         "date": "2026-07-03 12:30", "impact": "high"},
    {"event": "US CPI",      "date": "2026-07-10 12:30", "impact": "high"},
    {"event": "FOMC Meeting", "date": "2026-07-29 18:00", "impact": "high"},
    {"event": "FOMC Meeting", "date": "2026-07-31 18:00", "impact": "high"},
    # ── August 2026 ────────────────────────────────────────────────────────
    {"event": "NFP",         "date": "2026-08-07 12:30", "impact": "high"},
    {"event": "US CPI",      "date": "2026-08-12 12:30", "impact": "high"},
    # ── September 2026 ─────────────────────────────────────────────────────
    {"event": "NFP",         "date": "2026-09-04 12:30", "impact": "high"},
    {"event": "US CPI",      "date": "2026-09-11 12:30", "impact": "high"},
    {"event": "FOMC Meeting", "date": "2026-09-16 18:00", "impact": "high"},
]

# Internal parsed cache so we only convert strings once per process lifetime.
_PARSED_EVENTS: list[dict] | None = None


def _parse_events(raw: list[dict]) -> list[dict]:
    """Convert raw event dicts (date as string) to dicts with datetime objects (UTC)."""
    parsed: list[dict] = []
    for entry in raw:
        try:
            dt = datetime.strptime(entry["date"], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
            parsed.append(
                {
                    "event": entry["event"],
                    "datetime": dt,
                    "impact": entry["impact"],
                }
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed event entry %s: %s", entry, exc)
    return parsed


def _get_parsed_events() -> list[dict]:
    """Return lazily-parsed event list (cached after first call)."""
    global _PARSED_EVENTS
    if _PARSED_EVENTS is None:
        _PARSED_EVENTS = _parse_events(EVENTS_2026)
        logger.debug("Loaded %d macro events from hardcoded calendar.", len(_PARSED_EVENTS))
    return _PARSED_EVENTS


def get_upcoming_events(hours_ahead: int = 48) -> list[dict]:
    """
    Return high-impact macro events scheduled in the next ``hours_ahead`` hours.

    Args:
        hours_ahead: Look-ahead window in hours (default: 48).

    Returns:
        A list of event dicts sorted by ``hours_until`` ascending::

            [
                {
                    "event":      str,      # e.g. 'US CPI', 'FOMC Meeting', 'NFP'
                    "datetime":   datetime, # UTC-aware
                    "impact":     str,      # 'high'
                    "hours_until": float,   # hours from now until event
                },
                ...
            ]

        Returns an empty list when no events fall within the window.
    """
    now = datetime.now(tz=timezone.utc)
    upcoming: list[dict] = []

    for event in _get_parsed_events():
        delta_seconds = (event["datetime"] - now).total_seconds()
        hours_until = delta_seconds / 3600.0

        if 0 < hours_until < hours_ahead:
            upcoming.append(
                {
                    "event": event["event"],
                    "datetime": event["datetime"],
                    "impact": event["impact"],
                    "hours_until": round(hours_until, 4),
                }
            )

    upcoming.sort(key=lambda e: e["hours_until"])
    logger.debug(
        "get_upcoming_events(hours_ahead=%d) → %d event(s) found.",
        hours_ahead,
        len(upcoming),
    )
    return upcoming


def is_near_high_impact_event(hours_threshold: int = 2) -> bool:
    """
    Return ``True`` if any high-impact macro event falls within ``hours_threshold`` hours.

    This is the primary gate used by **Filter 7**: no new entries are opened
    when a high-impact event is imminent.

    Args:
        hours_threshold: Proximity window in hours (default: 2).

    Returns:
        ``True`` if at least one high-impact event is within the threshold,
        ``False`` otherwise.
    """
    events = get_upcoming_events(hours_ahead=hours_threshold)
    result = len(events) > 0
    if result:
        names = ", ".join(e["event"] for e in events)
        logger.info(
            "Filter 7 BLOCK — high-impact event(s) within %dh: %s",
            hours_threshold,
            names,
        )
    return result


# ---------------------------------------------------------------------------
# Quick smoke-test — run directly: python -m external.economic_calendar
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    print("\n=== Upcoming events (next 48 h) ===")
    for ev in get_upcoming_events(hours_ahead=48):
        print(
            f"  {ev['event']:<15}  {ev['datetime'].strftime('%Y-%m-%d %H:%M UTC')}"
            f"  ({ev['hours_until']:.1f}h away)"
        )
    print(f"\nFilter 7 block (2h threshold): {is_near_high_impact_event(2)}")
