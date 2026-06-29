"""
Telegram command handler for position tracking and statistics.

Handles bot commands: /status, /stats, /history, /positions, /close
"""

import asyncio
import logging
from typing import Optional

from signals.position_tracker import PositionTracker
from signals.win_rate import get_weekly_summary

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    """Handles Telegram bot commands for position tracking."""

    def __init__(self, db_path: str, position_tracker: PositionTracker, bot):
        """
        Initialize command handler.

        Args:
            db_path: Path to SQLite database
            position_tracker: PositionTracker instance
            bot: Telegram bot instance
        """
        self.db_path = db_path
        self.tracker = position_tracker
        self.bot = bot

    async def handle_command(self, command: str, args: list[str]) -> str:
        """
        Route command to appropriate handler.

        Args:
            command: Command name (without leading slash)
            args: List of command arguments

        Returns:
            Response text formatted as Telegram HTML
        """
        try:
            if command == 'status' or command == 'positions':
                return await self._status()
            elif command == 'stats':
                return await self._stats(args)
            elif command == 'history':
                return await self._history(args)
            elif command == 'close':
                return await self._close(args)
            else:
                return (
                    "<b>Available commands:</b>\n"
                    "/status - Show open positions\n"
                    "/stats - Show 7-day performance\n"
                    "/history [days] - Show closed positions\n"
                    "/positions - Alias for /status\n"
                    "/close &lt;id&gt; &lt;price&gt; - Manually close position"
                )
        except Exception as e:
            logger.error(f"Error handling command {command}: {e}", exc_info=True)
            return f"<b>Error:</b> {str(e)}"

    async def _status(self) -> str:
        """
        Format open positions as Telegram HTML.

        Returns:
            HTML-formatted string of open positions
        """
        positions = self.tracker.get_open_positions()
        if not positions:
            return "<b>No open positions</b>"

        lines = ["<b>📊 Open Positions</b>\n"]
        for pos in positions:
            lines.append(f"<b>#{pos['id']}</b> {pos['symbol']} {pos['direction']}")
            lines.append(
                f"Entry: ${pos['entry_price']:,.2f} | "
                f"Size: {pos['position_size']:.4f}"
            )
            lines.append(
                f"SL: ${pos['stop_loss']:,.2f} | "
                f"TP: ${pos['tp1']:,.2f}"
            )
            lines.append("")

        return "\n".join(lines)

    async def _stats(self, args: list[str]) -> str:
        """
        Show win rate statistics.

        Args:
            args: Command arguments (currently unused)

        Returns:
            HTML-formatted statistics summary
        """
        stats = get_weekly_summary(self.db_path)

        lines = ["<b>📈 7-Day Performance</b>\n"]
        lines.append(f"Total Trades: {stats['total_trades']}")
        lines.append(f"Win Rate: {stats['win_rate']:.1%}")
        lines.append(f"Avg R:R: {stats['avg_rr']:.2f}")
        lines.append(f"Profit Factor: {stats['profit_factor']:.2f}")
        lines.append(
            f"Total P&L: ${stats['total_pnl_usd']:,.2f} "
            f"({stats['total_pnl_pct']:.1f}%)"
        )

        return "\n".join(lines)

    async def _history(self, args: list[str]) -> str:
        """
        Show closed positions.

        Args:
            args: Command arguments [days] (default: 7)

        Returns:
            HTML-formatted closed positions history
        """
        days = int(args[0]) if args else 7
        positions = self.tracker.get_closed_positions(days=days)

        if not positions:
            return f"<b>No closed positions in last {days} days</b>"

        lines = [f"<b>📜 Last {days} Days</b>\n"]
        # Show last 10 positions
        for pos in positions[-10:]:
            status_emoji = "✅" if pos['status'] == 'CLOSED_WIN' else "❌"
            pnl_pct = pos.get('pnl_pct', 0)
            pnl_usd = pos.get('pnl_usd', 0)
            lines.append(
                f"{status_emoji} {pos['symbol']} {pos['direction']} | "
                f"P&L: {pnl_pct:.1f}% (${pnl_usd:.2f})"
            )

        return "\n".join(lines)

    async def _close(self, args: list[str]) -> str:
        """
        Manually close a position.

        Args:
            args: [position_id, price]

        Returns:
            HTML-formatted close confirmation
        """
        if len(args) < 2:
            return "<b>Usage:</b> /close &lt;position_id&gt; &lt;price&gt;"

        try:
            pos_id = int(args[0])
            price = float(args[1])
            result = self.tracker.close_position(pos_id, price, reason='manual')

            pnl_pct = result.get('pnl_pct', 0)
            pnl_usd = result.get('pnl_usd', 0)

            return (
                f"<b>Position #{pos_id} closed</b>\n"
                f"P&L: {pnl_pct:.1f}% (${pnl_usd:.2f})"
            )
        except ValueError as e:
            return f"<b>Invalid input:</b> Position ID and price must be numbers"
        except Exception as e:
            logger.error(f"Error closing position {args[0]}: {e}", exc_info=True)
            return f"<b>Error:</b> {str(e)}"
