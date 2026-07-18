"""SignalForge — Weekly P&L Report
Queries the local SQLite DB for the past 7 days of signal and position data,
builds a formatted HTML Telegram message, and sends it to the configured chat.

Usage (standalone):
    python3 -m reporting.weekly_report

Called by Hermes cron every Sunday at 08:00 UTC.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
_ROOT    = Path(__file__).parent.parent
_DB_PATH = _ROOT / "db" / "signalforge.db"


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def _week_range() -> tuple[str, str]:
    """Return ISO strings for the Mon–Sun calendar week containing 'now'.

    On Sunday the cron fires — we want Mon 00:00 through Sun 23:59:59 of the
    week that just ended, not a rolling 7-day window that drifts with restart
    time.  Using isocalendar() guarantees a clean Mon-based boundary.
    """
    now   = datetime.now(timezone.utc)
    # Monday of the current ISO week (may be today if it's Monday)
    days_since_monday = now.weekday()          # Mon=0 … Sun=6
    monday = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sunday_end = monday + timedelta(days=7) - timedelta(seconds=1)
    return monday.strftime("%Y-%m-%d %H:%M:%S"), sunday_end.strftime("%Y-%m-%d %H:%M:%S")


def _query_signals(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """All signals created in the window regardless of outcome."""
    return conn.execute(
        """
        SELECT symbol, direction, confidence, rr_ratio,
               filter_result, delivered, outcome, outcome_pnl_pct, outcome_r,
               created_at
        FROM   signals
        WHERE  created_at BETWEEN ? AND ?
        ORDER  BY created_at
        """,
        (start, end),
    ).fetchall()


def _query_positions(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """Closed positions in the window."""
    return conn.execute(
        """
        SELECT symbol, direction, outcome, pnl_pct, r_achieved,
               opened_at, closed_at
        FROM   positions
        WHERE  status = 'closed'
          AND  closed_at BETWEEN ? AND ?
        ORDER  BY closed_at
        """,
        (start, end),
    ).fetchall()


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def _compute_stats(
    signals: list[sqlite3.Row],
    positions: list[sqlite3.Row],
) -> dict:
    total_signals   = len(signals)
    delivered       = sum(1 for s in signals if s["delivered"])
    filtered_out    = total_signals - delivered

    # Outcome breakdown from signals table
    outcomes: dict[str, int] = {}
    for s in signals:
        o = s["outcome"] or "OPEN"
        outcomes[o] = outcomes.get(o, 0) + 1

    # P&L from closed positions
    closed_pnl = [float(p["pnl_pct"]) for p in positions if p["pnl_pct"] is not None]
    closed_r   = [float(p["r_achieved"]) for p in positions if p["r_achieved"] is not None]

    wins  = sum(1 for p in positions if (p["pnl_pct"] or 0) > 0)
    losses= sum(1 for p in positions if (p["pnl_pct"] or 0) < 0)
    total_closed = len(positions)

    win_rate    = (wins / total_closed * 100) if total_closed else 0.0
    total_pnl   = sum(closed_pnl)
    avg_r       = (sum(closed_r) / len(closed_r)) if closed_r else 0.0
    best_trade  = max(closed_pnl, default=0.0)
    worst_trade = min(closed_pnl, default=0.0)

    # Per-symbol breakdown
    sym_stats: dict[str, dict] = {}
    for p in positions:
        sym = p["symbol"]
        if sym not in sym_stats:
            sym_stats[sym] = {"wins": 0, "losses": 0, "pnl": 0.0}
        pnl = float(p["pnl_pct"] or 0)
        sym_stats[sym]["pnl"] += pnl
        if pnl > 0:
            sym_stats[sym]["wins"] += 1
        elif pnl < 0:
            sym_stats[sym]["losses"] += 1

    # Average confidence of delivered signals
    conf_vals = [float(s["confidence"]) for s in signals
                 if s["delivered"] and s["confidence"] is not None]
    avg_conf = (sum(conf_vals) / len(conf_vals)) if conf_vals else 0.0

    return {
        "total_signals":  total_signals,
        "delivered":      delivered,
        "filtered_out":   filtered_out,
        "outcomes":       outcomes,
        "total_closed":   total_closed,
        "wins":           wins,
        "losses":         losses,
        "win_rate":       win_rate,
        "total_pnl":      total_pnl,
        "avg_r":          avg_r,
        "best_trade":     best_trade,
        "worst_trade":    worst_trade,
        "sym_stats":      sym_stats,
        "avg_conf":       avg_conf,
    }


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _pnl_emoji(pnl: float) -> str:
    if pnl > 0:
        return "🟢"
    if pnl < 0:
        return "🔴"
    return "⚪"


def _format_message(stats: dict, start: str, end: str) -> str:
    s = stats
    period = f"{start[:10]} → {end[:10]}"

    # Outcome breakdown string
    outcome_lines = ""
    for outcome, count in sorted(s["outcomes"].items()):
        outcome_lines += f"  {outcome}: <b>{count}</b>\n"

    # Per-symbol table
    sym_lines = ""
    for sym, d in sorted(s["sym_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
        icon = _pnl_emoji(d["pnl"])
        sym_lines += (
            f"  {icon} {sym}: "
            f"W{d['wins']}/L{d['losses']} "
            f"| {'+' if d['pnl'] >= 0 else ''}{d['pnl']:.2f}%\n"
        )
    if not sym_lines:
        sym_lines = "  No closed positions this week\n"

    total_icon = _pnl_emoji(s["total_pnl"])

    msg = (
        f"📊 <b>SignalForge Weekly Report</b>\n"
        f"────────────────────\n"
        f"📅 Period: {period}\n\n"

        f"<b>Signal Pipeline</b>\n"
        f"  Generated:   <b>{s['total_signals']}</b>\n"
        f"  Delivered:   <b>{s['delivered']}</b>\n"
        f"  Filtered:    <b>{s['filtered_out']}</b>\n"
        f"  Avg Conf:    <b>{s['avg_conf']:.0f}%</b>\n\n"

        f"<b>Outcomes</b>\n"
        f"{outcome_lines}\n"

        f"<b>Closed Trades</b>\n"
        f"  Total:      <b>{s['total_closed']}</b>\n"
        f"  Wins/Loss:  <b>{s['wins']}W / {s['losses']}L</b>\n"
        f"  Win Rate:   <b>{s['win_rate']:.1f}%</b>\n"
        f"  Total P&L:  {total_icon} <b>{'+' if s['total_pnl'] >= 0 else ''}{s['total_pnl']:.2f}%</b>\n"
        f"  Avg R:      <b>{s['avg_r']:+.2f}R</b>\n"
        f"  Best:       🟢 <b>+{s['best_trade']:.2f}%</b>\n"
        f"  Worst:      🔴 <b>{s['worst_trade']:.2f}%</b>\n\n"

        f"<b>Per-Symbol</b>\n"
        f"{sym_lines}"
        f"────────────────────\n"
        f"<i>SignalForge v1 · Auto-report</i>"
    )
    return msg


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def send_weekly_report() -> bool:
    """Build and send the weekly report. Returns True on success."""
    # Import here so the module is usable standalone without the full app
    from delivery.telegram_bot import TelegramBot
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("[WeeklyReport] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    start, end = _week_range()
    logger.info("[WeeklyReport] Generating report for %s → %s", start, end)

    try:
        conn      = _db_connect()
        signals   = _query_signals(conn, start, end)
        positions = _query_positions(conn, start, end)
        conn.close()
    except Exception as e:
        logger.error("[WeeklyReport] DB query failed: %s", e)
        return False

    stats = _compute_stats(signals, positions)
    msg   = _format_message(stats, start, end)

    bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    try:
        ok = await bot.send(msg)
        if ok:
            logger.info("[WeeklyReport] Sent successfully")
        else:
            logger.error("[WeeklyReport] Telegram send returned False")
        return ok
    finally:
        await bot.close()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_ROOT))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(send_weekly_report())
