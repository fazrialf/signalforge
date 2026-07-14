"""
monitoring/weekly_report.py — Weekly performance report generator for SignalForge.

Generates comprehensive weekly performance reports combining:
- Trading performance statistics (win rate, P&L, R:R)
- Best/worst trade analysis
- Signal generation and filter statistics
- Delivered via Telegram with HTML formatting
"""
import sqlite3
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from signals.win_rate import get_weekly_summary, get_monthly_summary
from signals.position_tracker import PositionTracker

logger = logging.getLogger(__name__)


def get_filter_stats(db_path: str, days: int = 7) -> dict:
    """
    Query signal_log table for filter statistics over the specified period.
    
    Args:
        db_path: Path to SQLite database
        days: Number of days to look back (default: 7)
    
    Returns:
        Dictionary containing:
        - total_signals: Total signals evaluated
        - delivered: Signals that passed filters and were sent
        - filtered: Signals blocked by filters
        - pass_signals: Signals that were PASS (below threshold)
        - filter_pass_rate: Percentage of signals that passed filters
        - by_filter: Breakdown of which filters blocked signals
    """
    default_stats = {
        "total_signals": 0,
        "delivered": 0,
        "filtered": 0,
        "pass_signals": 0,
        "filter_pass_rate": 0.0,
        "by_filter": {},
    }
    
    try:
        # Calculate cutoff timestamp (days ago)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Total signals evaluated in period
        total = cursor.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at >= ?",
            (cutoff_str,)
        ).fetchone()[0]
        
        # Delivered signals (passed all filters)
        delivered = cursor.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at >= ? AND delivered = 1",
            (cutoff_str,)
        ).fetchone()[0]
        
        # PASS signals (below threshold, not actionable)
        pass_signals = cursor.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at >= ? AND direction = 'PASS'",
            (cutoff_str,)
        ).fetchone()[0]
        
        # Filtered signals (failed filters, excluding PASS)
        filtered = cursor.execute(
            """
            SELECT COUNT(*) FROM signals 
            WHERE created_at >= ? 
              AND delivered = 0 
              AND direction != 'PASS'
            """,
            (cutoff_str,)
        ).fetchone()[0]
        
        # Breakdown by filter (only for signals that were filtered)
        by_filter = {}
        filter_rows = cursor.execute(
            """
            SELECT filter_result, COUNT(*) as count
            FROM signals
            WHERE created_at >= ?
              AND delivered = 0
              AND direction != 'PASS'
              AND filter_result IS NOT NULL
            GROUP BY filter_result
            """,
            (cutoff_str,)
        ).fetchall()
        
        for row in filter_rows:
            filter_name = row['filter_result']
            # Extract filter name from "FAIL:<filter>" format
            if filter_name and filter_name.startswith('FAIL:'):
                filter_name = filter_name[5:]  # Remove "FAIL:" prefix
            by_filter[filter_name] = row['count']
        
        conn.close()
        
        # Calculate filter pass rate (exclude PASS signals from denominator)
        actionable_signals = total - pass_signals
        if actionable_signals > 0:
            filter_pass_rate = (delivered / actionable_signals) * 100
        else:
            filter_pass_rate = 0.0
        
        stats = {
            "total_signals": total,
            "delivered": delivered,
            "filtered": filtered,
            "pass_signals": pass_signals,
            "filter_pass_rate": round(filter_pass_rate, 1),
            "by_filter": by_filter,
        }
        
        logger.info(
            f"Filter stats calculated: {total} total, "
            f"{delivered} delivered, {filtered} filtered, "
            f"pass rate: {filter_pass_rate:.1f}%"
        )
        
        return stats
        
    except Exception as e:
        logger.error(f"Error calculating filter stats: {e}", exc_info=True)
        return default_stats


def format_direction(direction: str) -> str:
    """Convert BUY/SELL to LONG/SHORT for display."""
    return "LONG" if direction == "BUY" else "SHORT"


async def send_weekly_report(db_path: str, bot) -> bool:
    """
    Generate and send a comprehensive weekly performance report to Telegram.
    
    Combines trading performance metrics, best/worst trade analysis, and
    signal generation/filter statistics into a formatted HTML report.
    
    Args:
        db_path: Path to SQLite database
        bot: TelegramBot instance with send() method
    
    Returns:
        True if report was sent successfully, False otherwise
    """
    try:
        logger.info("Generating weekly performance report...")
        
        # Calculate date range for report header
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=7)
        date_range = (
            f"{start_date.strftime('%d %b %Y')} — "
            f"{end_date.strftime('%d %b %Y')}"
        )
        
        # Gather performance statistics
        perf_stats = get_weekly_summary(db_path)
        filter_stats = get_filter_stats(db_path, days=7)
        
        # Build the report
        lines = [
            "📊 <b>Weekly SignalForge Report</b>",
            f"<i>Week of {date_range}</i>",
            "",
            "<b>Performance</b>",
        ]
        
        # Performance metrics
        if perf_stats['total_trades'] == 0:
            lines.append("• No closed trades this week")
        else:
            lines.extend([
                f"• Total Trades: {perf_stats['total_trades']}",
                f"• Win Rate: {perf_stats['win_rate']:.1f}%",
                f"• Avg R:R: {perf_stats['avg_rr']:.2f}",
                f"• Profit Factor: {perf_stats['profit_factor']:.2f}",
                f"• Total P&L: ${perf_stats['total_pnl_usd']:.2f} ({perf_stats['total_pnl_pct']:+.1f}%)",
            ])
        
        # Best trade
        if perf_stats.get('best_trade'):
            bt = perf_stats['best_trade']
            lines.extend([
                "",
                "<b>Best Trade</b>",
                f"• {bt['symbol']} +{bt['pnl_pct']:.1f}% (R:R {bt['rr_realized']:.2f})",
            ])
        
        # Worst trade
        if perf_stats.get('worst_trade'):
            wt = perf_stats['worst_trade']
            lines.extend([
                "",
                "<b>Worst Trade</b>",
                f"• {wt['symbol']} {wt['pnl_pct']:.1f}%",
            ])
        
        # Filter statistics
        lines.extend([
            "",
            "<b>Signal Generation</b>",
            f"• Signals evaluated: {filter_stats['total_signals']}",
            f"• Signals delivered: {filter_stats['delivered']}",
            f"• Filter pass rate: {filter_stats['filter_pass_rate']:.1f}%",
        ])
        
        # Signal log breakdown
        lines.extend([
            "",
            "<b>Signal Breakdown</b>",
            f"• PASS (below threshold): {filter_stats['pass_signals']}",
            f"• Blocked by filters: {filter_stats['filtered']}",
            f"• Delivered: {filter_stats['delivered']}",
        ])
        
        # Top filters (if any signals were filtered)
        if filter_stats['by_filter']:
            lines.append("")
            lines.append("<b>Top Filters</b>")
            # Sort by count, take top 3
            sorted_filters = sorted(
                filter_stats['by_filter'].items(),
                key=lambda x: x[1],
                reverse=True
            )[:3]
            for filter_name, count in sorted_filters:
                lines.append(f"• {filter_name}: {count}")
        
        # Footer
        next_report = end_date + timedelta(days=7)
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<i>Next report: {next_report.strftime('%d %b %Y')}</i>",
        ])
        
        report_text = "\n".join(lines)
        
        # Send via Telegram
        success = await bot.send(report_text, parse_mode="HTML")
        
        if success:
            logger.info("Weekly report sent successfully")
        else:
            logger.error("Failed to send weekly report")
        
        return success
        
    except Exception as e:
        logger.error(f"Error generating weekly report: {e}", exc_info=True)
        return False


async def send_monthly_report(db_path: str, bot) -> bool:
    """
    Generate and send a monthly performance report to Telegram.
    
    Similar to weekly report but covers 30-day period.
    
    Args:
        db_path: Path to SQLite database
        bot: TelegramBot instance with send() method
    
    Returns:
        True if report was sent successfully, False otherwise
    """
    try:
        logger.info("Generating monthly performance report...")
        
        # Calculate date range
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)
        date_range = (
            f"{start_date.strftime('%d %b %Y')} — "
            f"{end_date.strftime('%d %b %Y')}"
        )
        
        # Import monthly stats function
        from signals.win_rate import get_monthly_summary
        
        # Gather statistics
        perf_stats = get_monthly_summary(db_path)
        filter_stats = get_filter_stats(db_path, days=30)
        
        # Build report
        lines = [
            "📊 <b>Monthly SignalForge Report</b>",
            f"<i>{date_range}</i>",
            "",
            "<b>Performance</b>",
        ]
        
        if perf_stats['total_trades'] == 0:
            lines.append("• No closed trades this month")
        else:
            lines.extend([
                f"• Total Trades: {perf_stats['total_trades']}",
                f"• Win Rate: {perf_stats['win_rate']:.1f}%",
                f"• Avg R:R: {perf_stats['avg_rr']:.2f}",
                f"• Profit Factor: {perf_stats['profit_factor']:.2f}",
                f"• Total P&L: ${perf_stats['total_pnl_usd']:.2f} ({perf_stats['total_pnl_pct']:+.1f}%)",
                f"• Avg Hold Time: {perf_stats['avg_hold_time_hours']:.1f}h",
            ])
        
        if perf_stats.get('best_trade'):
            bt = perf_stats['best_trade']
            lines.extend([
                "",
                "<b>Best Trade</b>",
                f"• {bt['symbol']} +{bt['pnl_pct']:.1f}% (R:R {bt['rr_realized']:.2f})",
            ])
        
        if perf_stats.get('worst_trade'):
            wt = perf_stats['worst_trade']
            lines.extend([
                "",
                "<b>Worst Trade</b>",
                f"• {wt['symbol']} {wt['pnl_pct']:.1f}%",
            ])
        
        lines.extend([
            "",
            "<b>Signal Generation</b>",
            f"• Signals evaluated: {filter_stats['total_signals']}",
            f"• Signals delivered: {filter_stats['delivered']}",
            f"• Filter pass rate: {filter_stats['filter_pass_rate']:.1f}%",
        ])
        
        report_text = "\n".join(lines)
        
        success = await bot.send(report_text, parse_mode="HTML")
        
        if success:
            logger.info("Monthly report sent successfully")
        else:
            logger.error("Failed to send monthly report")
        
        return success
        
    except Exception as e:
        logger.error(f"Error generating monthly report: {e}", exc_info=True)
        return False
