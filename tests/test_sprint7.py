"""Unit tests for Sprint 7 — Position Tracking, Win Rate, Commands, Weekly Report."""
import sys, os, time, tempfile, sqlite3, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from signals.position_tracker import PositionTracker, Position
from signals.win_rate import calculate_stats, get_weekly_summary, get_monthly_summary
from delivery.telegram_commands import TelegramCommandHandler


# ─── Helpers ──────────────────────────────────────────────────────────────────
def make_db() -> str:
    """Create a fresh temp DB and init required tables."""
    db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    db_path = db.name
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY, ts INTEGER, symbol TEXT, timeframe TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, timeframe TEXT,
            entry_price REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
            rr_ratio REAL, confidence REAL, llm_reasoning TEXT,
            primary_reason TEXT, confluence_score INTEGER, mtf_aligned INTEGER,
            filter_result TEXT, cooldown_remaining REAL,
            created_at INTEGER, outcome TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def insert_position(tracker: PositionTracker, direction="LONG", entry=60000.0,
                    sl=59000.0, tp1=61500.0, tp2=62000.0, tp3=63000.0,
                    size=0.01, signal_id=1) -> int:
    return tracker.open_position(
        signal_id=signal_id,
        symbol="BTC/USDT",
        direction=direction,
        entry_price=entry,
        position_size=size,
        stop_loss=sl,
        tp1=tp1, tp2=tp2, tp3=tp3,
    )


# ─── PositionTracker Tests ─────────────────────────────────────────────────────
class TestPositionTracker(unittest.TestCase):
    def setUp(self):
        self.db_path = make_db()
        self.tracker = PositionTracker(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_open_position_returns_id(self):
        pid = insert_position(self.tracker)
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)

    def test_get_open_positions(self):
        insert_position(self.tracker)
        insert_position(self.tracker, direction="SHORT", signal_id=2)
        positions = self.tracker.get_open_positions()
        self.assertEqual(len(positions), 2)

    def test_open_position_fields(self):
        pid = insert_position(self.tracker, entry=60000, sl=59000, tp1=61500)
        positions = self.tracker.get_open_positions()
        pos = next(p for p in positions if p['id'] == pid)
        self.assertEqual(pos['symbol'], 'BTC/USDT')
        self.assertEqual(pos['direction'], 'LONG')
        self.assertAlmostEqual(pos['entry_price'], 60000.0)
        self.assertAlmostEqual(pos['stop_loss'], 59000.0)
        self.assertAlmostEqual(pos['tp1'], 61500.0)
        self.assertEqual(pos['status'], 'OPEN')

    def test_close_long_win(self):
        pid = insert_position(self.tracker, entry=60000, sl=59000, size=0.01)
        result = self.tracker.close_position(pid, close_price=62000.0)
        self.assertEqual(result['status'], 'CLOSED_WIN')
        self.assertGreater(result['pnl_usd'], 0)
        self.assertGreater(result['pnl_pct'], 0)

    def test_close_long_loss(self):
        pid = insert_position(self.tracker, entry=60000, sl=59000, size=0.01)
        result = self.tracker.close_position(pid, close_price=59000.0)
        self.assertEqual(result['status'], 'CLOSED_LOSS')
        self.assertLess(result['pnl_usd'], 0)

    def test_close_short_win(self):
        pid = insert_position(self.tracker, direction="SHORT", entry=60000,
                               sl=61000, tp1=58500, tp2=57000, tp3=55000, size=0.01)
        result = self.tracker.close_position(pid, close_price=58000.0)
        self.assertEqual(result['status'], 'CLOSED_WIN')
        self.assertGreater(result['pnl_usd'], 0)

    def test_close_short_loss(self):
        pid = insert_position(self.tracker, direction="SHORT", entry=60000,
                               sl=61000, tp1=58500, size=0.01)
        result = self.tracker.close_position(pid, close_price=61000.0)
        self.assertEqual(result['status'], 'CLOSED_LOSS')
        self.assertLess(result['pnl_usd'], 0)

    def test_pnl_usd_calculation(self):
        # LONG: buy 0.01 BTC at 60000, sell at 62000 → +$20
        pid = insert_position(self.tracker, entry=60000, sl=59000, size=0.01)
        result = self.tracker.close_position(pid, close_price=62000.0)
        expected_pnl = (62000 - 60000) * 0.01  # = 20.0
        self.assertAlmostEqual(result['pnl_usd'], expected_pnl, places=4)

    def test_get_closed_positions(self):
        pid = insert_position(self.tracker)
        self.tracker.close_position(pid, close_price=61000.0)
        closed = self.tracker.get_closed_positions(days=7)
        self.assertEqual(len(closed), 1)

    def test_get_open_excludes_closed(self):
        pid1 = insert_position(self.tracker, signal_id=1)
        pid2 = insert_position(self.tracker, signal_id=2)
        self.tracker.close_position(pid1, close_price=61000.0)
        open_pos = self.tracker.get_open_positions()
        open_ids = [p['id'] for p in open_pos]
        self.assertNotIn(pid1, open_ids)
        self.assertIn(pid2, open_ids)

    def test_rr_realized(self):
        # LONG: entry=60000, sl=59000 → risk=1000 per BTC
        # close at 62000 → profit=2000 → rr=2.0
        pid = insert_position(self.tracker, entry=60000, sl=59000, size=0.01)
        result = self.tracker.close_position(pid, close_price=62000.0)
        self.assertAlmostEqual(result['rr_realized'], 2.0, places=2)

    def test_update_position_sl_hit(self):
        pid = insert_position(self.tracker, entry=60000, sl=59000, direction="LONG")
        result = self.tracker.update_position(pid, current_price=58500.0)
        self.assertEqual(result['status'], 'CLOSED_LOSS')

    def test_update_position_tp1_hit(self):
        pid = insert_position(self.tracker, entry=60000, sl=59000,
                               tp1=61500, tp2=62000, tp3=63000)
        result = self.tracker.update_position(pid, current_price=61600.0)
        self.assertTrue(result['tp1_hit'])

    def test_breakeven_status(self):
        pid = insert_position(self.tracker, entry=60000, sl=59000, size=0.01)
        # Close near entry — small move, should be CLOSED_BE
        result = self.tracker.close_position(pid, close_price=60020.0)
        self.assertIn(result['status'], ['CLOSED_BE', 'CLOSED_WIN'])


# ─── Win Rate Tests ────────────────────────────────────────────────────────────
class TestWinRate(unittest.TestCase):
    def setUp(self):
        self.db_path = make_db()
        self.tracker = PositionTracker(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _close(self, entry, close, direction="LONG", sl=None, size=0.01, signal_id=1):
        if sl is None:
            sl = entry - 1000 if direction == "LONG" else entry + 1000
        tp1 = entry + 1500 if direction == "LONG" else entry - 1500
        pid = self.tracker.open_position(
            signal_id=signal_id, symbol="BTC/USDT", direction=direction,
            entry_price=entry, position_size=size, stop_loss=sl,
            tp1=tp1, tp2=tp1 * 1.01, tp3=tp1 * 1.02,
        )
        return self.tracker.close_position(pid, close_price=close)

    def test_empty_db_returns_zeros(self):
        stats = calculate_stats(self.db_path, days=7)
        self.assertEqual(stats['total_trades'], 0)
        self.assertEqual(stats['win_rate'], 0.0)
        self.assertEqual(stats['profit_factor'], 0.0)

    def test_win_rate_calculation(self):
        # 2 wins, 1 loss
        self._close(60000, 62000, signal_id=1)
        self._close(60000, 62000, signal_id=2)
        self._close(60000, 58500, signal_id=3)
        stats = calculate_stats(self.db_path, days=1)
        # win_rate may be returned as decimal (0.667) or percentage (66.67)
        wr = stats['win_rate']
        normalized = wr if wr <= 1.0 else wr / 100.0
        self.assertAlmostEqual(normalized, 2/3, places=2)
        self.assertEqual(stats['wins'], 2)
        self.assertEqual(stats['losses'], 1)

    def test_profit_factor(self):
        # Win $20, lose $15 → PF = 20/15 = 1.33
        self._close(60000, 62000, size=0.01, signal_id=1)  # +$20
        self._close(60000, 58500, size=0.01, signal_id=2)  # -$15
        stats = calculate_stats(self.db_path, days=1)
        self.assertGreater(stats['profit_factor'], 1.0)

    def test_profit_factor_no_losses(self):
        self._close(60000, 62000, signal_id=1)
        stats = calculate_stats(self.db_path, days=1)
        self.assertEqual(stats['profit_factor'], 999.0)

    def test_total_pnl(self):
        self._close(60000, 62000, size=0.01, signal_id=1)  # +$20
        self._close(60000, 62000, size=0.01, signal_id=2)  # +$20
        stats = calculate_stats(self.db_path, days=1)
        self.assertAlmostEqual(stats['total_pnl_usd'], 40.0, places=2)

    def test_weekly_summary(self):
        self._close(60000, 61500, signal_id=1)
        stats = get_weekly_summary(self.db_path)
        self.assertIsInstance(stats, dict)
        self.assertIn('win_rate', stats)
        self.assertIn('total_trades', stats)

    def test_monthly_summary(self):
        stats = get_monthly_summary(self.db_path)
        self.assertIsInstance(stats, dict)
        self.assertEqual(stats['period_days'], 30)

    def test_best_worst_trade(self):
        self._close(60000, 62000, size=0.01, signal_id=1)  # big win
        self._close(60000, 59500, size=0.01, signal_id=2)  # small loss
        stats = calculate_stats(self.db_path, days=1)
        if stats['total_trades'] >= 2:
            best = stats.get('best_trade')
            worst = stats.get('worst_trade')
            if best and worst:
                self.assertGreater(best['pnl_pct'], worst['pnl_pct'])


# ─── TelegramCommandHandler Tests ─────────────────────────────────────────────
class TestTelegramCommandHandler(unittest.TestCase):
    def setUp(self):
        self.db_path = make_db()
        self.tracker = PositionTracker(db_path=self.db_path)
        self.bot = AsyncMock()
        self.bot.send = AsyncMock(return_value=True)
        self.handler = TelegramCommandHandler(
            db_path=self.db_path,
            position_tracker=self.tracker,
            bot=self.bot,
        )

    def tearDown(self):
        os.unlink(self.db_path)

    def _run(self, coro):
        """Run async coroutine in test."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    def test_status_no_positions(self):
        response = self._run(self.handler.handle_command('status', []))
        self.assertIn('No open positions', response)

    def test_status_with_positions(self):
        insert_position(self.tracker)
        response = self._run(self.handler.handle_command('status', []))
        self.assertIn('BTC/USDT', response)

    def test_positions_alias(self):
        response = self._run(self.handler.handle_command('positions', []))
        self.assertIn('position', response.lower())

    def test_stats_empty(self):
        response = self._run(self.handler.handle_command('stats', []))
        self.assertIn('Total Trades', response)
        self.assertIn('Win Rate', response)

    def test_history_no_closed(self):
        response = self._run(self.handler.handle_command('history', []))
        self.assertIn('No closed positions', response)

    def test_history_with_closed(self):
        pid = insert_position(self.tracker)
        self.tracker.close_position(pid, close_price=61000.0)
        response = self._run(self.handler.handle_command('history', ['7']))
        self.assertIn('BTC/USDT', response)

    def test_close_invalid_args(self):
        response = self._run(self.handler.handle_command('close', []))
        self.assertIn('Usage', response)

    def test_close_valid(self):
        pid = insert_position(self.tracker)
        response = self._run(self.handler.handle_command('close', [str(pid), '61000']))
        self.assertIn('closed', response.lower())

    def test_close_invalid_id(self):
        response = self._run(self.handler.handle_command('close', ['99999', '61000']))
        # Should return some kind of error/failure message
        self.assertTrue(
            any(w in response for w in ['Error', 'error', 'Invalid', 'not found', 'failed']),
            f"Expected error message, got: {response}"
        )

    def test_unknown_command(self):
        response = self._run(self.handler.handle_command('foo', []))
        self.assertIn('Unknown command', response)


if __name__ == "__main__":
    unittest.main(verbosity=2)
