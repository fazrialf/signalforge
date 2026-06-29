"""
Trading performance statistics calculator.

Calculates win rate, average R:R, profit factor, and other metrics
from closed positions stored in the database.
"""

import sqlite3
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def calculate_stats(db_path: str, days: int = 30) -> dict:
    """
    Calculate performance stats from closed positions in last N days.
    
    Args:
        db_path: Path to SQLite database
        days: Number of days to look back (default: 30)
    
    Returns:
        Dictionary containing:
        - total_trades: Total number of closed trades
        - wins: Number of winning trades
        - losses: Number of losing trades
        - breakevens: Number of breakeven trades
        - win_rate: Win rate as percentage (wins / (wins + losses))
        - avg_rr: Average risk:reward ratio on winning trades
        - avg_win_pct: Average percentage gain on wins
        - avg_loss_pct: Average percentage loss on losses
        - profit_factor: Total wins USD / total losses USD
        - total_pnl_usd: Total profit/loss in USD
        - total_pnl_pct: Total profit/loss in percentage
        - best_trade: Best trade details {symbol, pnl_pct, rr_realized}
        - worst_trade: Worst trade details
        - avg_hold_time_hours: Average holding time in hours
        - period_days: Period analyzed
    """
    default_stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "breakevens": 0,
        "win_rate": 0.0,
        "avg_rr": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
        "profit_factor": 0.0,
        "total_pnl_usd": 0.0,
        "total_pnl_pct": 0.0,
        "best_trade": None,
        "worst_trade": None,
        "avg_hold_time_hours": 0.0,
        "period_days": days,
    }
    
    try:
        # Calculate cutoff timestamp (days ago in milliseconds)
        cutoff_ms = int((time.time() - (days * 24 * 3600)) * 1000)
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Query closed positions within the period
        query = """
            SELECT 
                symbol,
                status,
                entry_time,
                close_time,
                pnl_usd,
                pnl_pct,
                rr_realized
            FROM positions
            WHERE status IN ('CLOSED_WIN', 'CLOSED_LOSS', 'CLOSED_BE')
            AND close_time >= ?
            ORDER BY close_time DESC
        """
        
        cursor.execute(query, (cutoff_ms,))
        trades = cursor.fetchall()
        conn.close()
        
        if not trades:
            logger.info(f"No closed trades found in last {days} days")
            return default_stats
        
        # Initialize counters
        wins = []
        losses = []
        breakevens = []
        total_pnl_usd = 0.0
        total_pnl_pct = 0.0
        hold_times = []
        best_trade = None
        worst_trade = None
        
        # Process each trade
        for trade in trades:
            status = trade['status']
            pnl_usd = trade['pnl_usd'] or 0.0
            pnl_pct = trade['pnl_pct'] or 0.0
            
            # Calculate hold time
            if trade['entry_time'] and trade['close_time']:
                hold_time_ms = trade['close_time'] - trade['entry_time']
                hold_time_hours = hold_time_ms / (1000 * 3600)
                hold_times.append(hold_time_hours)
            
            # Categorize trade
            if status == 'CLOSED_WIN':
                wins.append(trade)
            elif status == 'CLOSED_LOSS':
                losses.append(trade)
            elif status == 'CLOSED_BE':
                breakevens.append(trade)
            
            # Accumulate totals
            total_pnl_usd += pnl_usd
            total_pnl_pct += pnl_pct
            
            # Track best/worst trades
            trade_dict = {
                "symbol": trade['symbol'],
                "pnl_pct": pnl_pct,
                "rr_realized": trade['rr_realized']
            }
            
            if best_trade is None or pnl_pct > best_trade['pnl_pct']:
                best_trade = trade_dict
            
            if worst_trade is None or pnl_pct < worst_trade['pnl_pct']:
                worst_trade = trade_dict
        
        # Calculate statistics
        total_trades = len(trades)
        num_wins = len(wins)
        num_losses = len(losses)
        num_breakevens = len(breakevens)
        
        # Win rate (exclude breakevens)
        decisive_trades = num_wins + num_losses
        win_rate = (num_wins / decisive_trades * 100) if decisive_trades > 0 else 0.0
        
        # Average R:R on winning trades
        winning_rr_values = [
            trade['rr_realized'] 
            for trade in wins 
            if trade['rr_realized'] is not None
        ]
        avg_rr = sum(winning_rr_values) / len(winning_rr_values) if winning_rr_values else 0.0
        
        # Average win/loss percentages
        win_pcts = [trade['pnl_pct'] for trade in wins if trade['pnl_pct']]
        avg_win_pct = sum(win_pcts) / len(win_pcts) if win_pcts else 0.0
        
        loss_pcts = [trade['pnl_pct'] for trade in losses if trade['pnl_pct']]
        avg_loss_pct = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0.0
        
        # Profit factor
        total_wins_usd = sum(trade['pnl_usd'] for trade in wins if trade['pnl_usd'])
        total_losses_usd = abs(sum(trade['pnl_usd'] for trade in losses if trade['pnl_usd']))
        
        if total_losses_usd == 0:
            # No losses - cap at 999.0
            profit_factor = 999.0 if total_wins_usd > 0 else 0.0
        else:
            profit_factor = total_wins_usd / total_losses_usd
        
        # Average hold time
        avg_hold_time = sum(hold_times) / len(hold_times) if hold_times else 0.0
        
        stats = {
            "total_trades": total_trades,
            "wins": num_wins,
            "losses": num_losses,
            "breakevens": num_breakevens,
            "win_rate": round(win_rate, 2),
            "avg_rr": round(avg_rr, 2),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": round(profit_factor, 2),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "avg_hold_time_hours": round(avg_hold_time, 2),
            "period_days": days,
        }
        
        logger.info(
            f"Stats calculated: {total_trades} trades, "
            f"Win rate: {win_rate:.1f}%, "
            f"Profit factor: {profit_factor:.2f}"
        )
        
        return stats
        
    except Exception as e:
        logger.error(f"Error calculating stats: {e}", exc_info=True)
        return default_stats


def get_weekly_summary(db_path: str) -> dict:
    """
    Calculate statistics for the last 7 days.
    
    Args:
        db_path: Path to SQLite database
    
    Returns:
        Performance statistics dictionary for weekly period
    """
    return calculate_stats(db_path, days=7)


def get_monthly_summary(db_path: str) -> dict:
    """
    Calculate statistics for the last 30 days.
    
    Args:
        db_path: Path to SQLite database
    
    Returns:
        Performance statistics dictionary for monthly period
    """
    return calculate_stats(db_path, days=30)


def format_stats_report(stats: dict) -> str:
    """
    Format statistics dictionary into a human-readable report.
    
    Args:
        stats: Statistics dictionary from calculate_stats()
    
    Returns:
        Formatted multi-line string report
    """
    if stats['total_trades'] == 0:
        return f"No trades found in the last {stats['period_days']} days."
    
    report_lines = [
        f"📊 **Performance Report ({stats['period_days']} days)**",
        "",
        f"Total Trades: {stats['total_trades']}",
        f"Wins: {stats['wins']} | Losses: {stats['losses']} | Breakevens: {stats['breakevens']}",
        f"Win Rate: {stats['win_rate']:.1f}%",
        "",
        f"Average R:R: {stats['avg_rr']:.2f}",
        f"Average Win: +{stats['avg_win_pct']:.2f}%",
        f"Average Loss: {stats['avg_loss_pct']:.2f}%",
        f"Profit Factor: {stats['profit_factor']:.2f}",
        "",
        f"Total P&L: ${stats['total_pnl_usd']:.2f} ({stats['total_pnl_pct']:+.2f}%)",
        f"Avg Hold Time: {stats['avg_hold_time_hours']:.1f}h",
    ]
    
    if stats['best_trade']:
        bt = stats['best_trade']
        report_lines.extend([
            "",
            f"🏆 Best: {bt['symbol']} +{bt['pnl_pct']:.2f}% (R:R {bt['rr_realized']})"
        ])
    
    if stats['worst_trade']:
        wt = stats['worst_trade']
        report_lines.extend([
            f"📉 Worst: {wt['symbol']} {wt['pnl_pct']:.2f}% (R:R {wt['rr_realized']})"
        ])
    
    return "\n".join(report_lines)
