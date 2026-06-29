"""Unit tests for Sprint 8 — Paper Trading, Multi-Asset Config, Stress Test."""
import sys, os, time, tempfile, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from trading.paper_trade import PaperTradeEngine
from config.assets import (
    AssetConfig, ASSETS, PAPER_MODE,
    get_asset, get_enabled_assets, get_all_symbols, get_all_binance_symbols,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def make_engine(balance=10000.0) -> tuple:
    db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    db_path = db.name
    db.close()
    engine = PaperTradeEngine(db_path=db_path, initial_balance=balance)
    return engine, db_path


def open_long(engine, entry=60000.0, sl=59000.0, tp1=61500.0,
              tp2=62000.0, tp3=63000.0, size=0.01, signal_id=1):
    return engine.open_trade(
        symbol="BTC/USDT", direction="LONG", entry=entry,
        sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, size=size,
        signal_id=signal_id, confidence=0.8,
    )


def open_short(engine, entry=60000.0, sl=61000.0, tp1=58500.0,
               tp2=58000.0, tp3=57000.0, size=0.01, signal_id=2):
    return engine.open_trade(
        symbol="BTC/USDT", direction="SHORT", entry=entry,
        sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, size=size,
        signal_id=signal_id, confidence=0.8,
    )


# ─── PaperTradeEngine Tests ────────────────────────────────────────────────────
class TestPaperTradeEngine(unittest.TestCase):
    def setUp(self):
        self.engine, self.db_path = make_engine(balance=10000.0)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_open_trade_returns_id(self):
        tid = open_long(self.engine)
        self.assertIsInstance(tid, int)
        self.assertGreater(tid, 0)

    def test_initial_balance(self):
        self.assertAlmostEqual(self.engine.get_balance(), 10000.0)

    def test_get_open_trades_empty(self):
        self.assertEqual(len(self.engine.get_open_trades()), 0)

    def test_get_open_trades_after_open(self):
        open_long(self.engine)
        self.assertEqual(len(self.engine.get_open_trades()), 1)

    def test_tick_no_trigger(self):
        open_long(self.engine, entry=60000, sl=59000, tp1=61500)
        closed = self.engine.tick("BTC/USDT", 60500.0)  # price between entry and tp1
        self.assertEqual(len(closed), 0)
        self.assertEqual(len(self.engine.get_open_trades()), 1)

    def test_tick_sl_hit_long(self):
        open_long(self.engine, entry=60000, sl=59000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 58900.0)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['status'], 'CLOSED_LOSS')
        self.assertLess(closed[0]['pnl_usd'], 0)

    def test_tick_tp3_hit_long(self):
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 63100.0)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['status'], 'CLOSED_WIN')
        self.assertGreater(closed[0]['pnl_usd'], 0)

    def test_tick_sl_hit_short(self):
        open_short(self.engine, entry=60000, sl=61000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 61100.0)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['status'], 'CLOSED_LOSS')
        self.assertLess(closed[0]['pnl_usd'], 0)

    def test_tick_tp3_hit_short(self):
        open_short(self.engine, entry=60000, sl=61000, tp3=57000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 56900.0)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['status'], 'CLOSED_WIN')
        self.assertGreater(closed[0]['pnl_usd'], 0)

    def test_balance_updates_on_win(self):
        initial = self.engine.get_balance()
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01)
        self.engine.tick("BTC/USDT", 63100.0)
        new_balance = self.engine.get_balance()
        self.assertGreater(new_balance, initial)

    def test_balance_updates_on_loss(self):
        initial = self.engine.get_balance()
        open_long(self.engine, entry=60000, sl=59000, size=0.01)
        self.engine.tick("BTC/USDT", 58900.0)
        new_balance = self.engine.get_balance()
        self.assertLess(new_balance, initial)

    def test_pnl_long_win_calculation(self):
        # LONG: buy 0.01 BTC @ 60000, close @ 63000 → +$30
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 63100.0)
        expected = (63000 - 60000) * 0.01  # = $30
        self.assertAlmostEqual(closed[0]['pnl_usd'], expected, places=2)

    def test_pnl_long_loss_calculation(self):
        # LONG: buy 0.01 @ 60000, SL @ 59000 → -$10
        open_long(self.engine, entry=60000, sl=59000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 58900.0)
        expected = (59000 - 60000) * 0.01  # = -$10
        self.assertAlmostEqual(closed[0]['pnl_usd'], expected, places=2)

    def test_tp1_tp2_marked_but_not_closed(self):
        tid = open_long(self.engine, entry=60000, sl=59000,
                        tp1=61500, tp2=62000, tp3=63000, size=0.01)
        self.engine.tick("BTC/USDT", 61600.0)  # above tp1
        trades = self.engine.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0].get('tp1_hit'))

    def test_get_closed_trades(self):
        open_long(self.engine, entry=60000, sl=59000, size=0.01, signal_id=1)
        self.engine.tick("BTC/USDT", 58900.0)
        closed = self.engine.get_closed_trades(days=7)
        self.assertEqual(len(closed), 1)

    def test_get_paper_stats_empty(self):
        stats = self.engine.get_paper_stats(days=7)
        self.assertEqual(stats['total_trades'], 0)
        self.assertEqual(stats['win_rate'], 0.0)

    def test_get_paper_stats_with_trades(self):
        # 2 wins, 1 loss
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01, signal_id=1)
        self.engine.tick("BTC/USDT", 63100.0)
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01, signal_id=2)
        self.engine.tick("BTC/USDT", 63100.0)
        open_long(self.engine, entry=60000, sl=59000, size=0.01, signal_id=3)
        self.engine.tick("BTC/USDT", 58900.0)

        stats = self.engine.get_paper_stats(days=1)
        self.assertEqual(stats['total_trades'], 3)
        self.assertEqual(stats['wins'], 2)
        self.assertEqual(stats['losses'], 1)
        wr = stats['win_rate']
        normalized = wr if wr <= 1.0 else wr / 100.0
        self.assertAlmostEqual(normalized, 2/3, places=2)

    def test_profit_factor(self):
        # Win $30, lose $10 → PF = 3.0
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01, signal_id=1)
        self.engine.tick("BTC/USDT", 63100.0)  # +$30
        open_long(self.engine, entry=60000, sl=59000, size=0.01, signal_id=2)
        self.engine.tick("BTC/USDT", 58900.0)  # -$10
        stats = self.engine.get_paper_stats(days=1)
        self.assertGreater(stats['profit_factor'], 1.0)

    def test_profit_factor_no_losses(self):
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01, signal_id=1)
        self.engine.tick("BTC/USDT", 63100.0)
        stats = self.engine.get_paper_stats(days=1)
        self.assertAlmostEqual(stats['profit_factor'], 999.0)

    def test_reset(self):
        open_long(self.engine, entry=60000, sl=59000, size=0.01)
        self.engine.reset()
        self.assertEqual(len(self.engine.get_open_trades()), 0)
        self.assertAlmostEqual(self.engine.get_balance(), 10000.0)

    def test_multiple_symbols(self):
        self.engine.open_trade("BTC/USDT", "LONG", 60000, 59000, 61500, 62000, 63000, 0.01, 1, 0.8)
        self.engine.open_trade("ETH/USDT", "LONG", 3000, 2950, 3075, 3100, 3150, 0.1, 2, 0.8)
        btc_closed = self.engine.tick("BTC/USDT", 58900.0)
        eth_open = self.engine.get_open_trades()
        # BTC closed by SL, ETH still open
        self.assertEqual(len(btc_closed), 1)
        eth_trades = [t for t in eth_open if t['symbol'] == 'ETH/USDT']
        self.assertEqual(len(eth_trades), 1)

    def test_rr_realized(self):
        # LONG: entry=60000, sl=59000 → risk=$10 per 0.01 BTC
        # close at tp3=63000 → profit=$30 → rr=3.0
        open_long(self.engine, entry=60000, sl=59000, tp3=63000, size=0.01)
        closed = self.engine.tick("BTC/USDT", 63100.0)
        self.assertAlmostEqual(closed[0]['rr_realized'], 3.0, places=1)


# ─── Multi-Asset Config Tests ──────────────────────────────────────────────────
class TestAssetConfig(unittest.TestCase):
    def test_assets_list_not_empty(self):
        self.assertGreater(len(ASSETS), 0)

    def test_btc_in_assets(self):
        symbols = get_all_symbols()
        self.assertIn("BTC/USDT", symbols)

    def test_get_asset_found(self):
        asset = get_asset("BTC/USDT")
        self.assertIsNotNone(asset)
        self.assertEqual(asset.symbol, "BTC/USDT")

    def test_get_asset_not_found(self):
        asset = get_asset("DOGE/USDT")
        self.assertIsNone(asset)

    def test_get_enabled_assets(self):
        enabled = get_enabled_assets()
        self.assertGreater(len(enabled), 0)
        for a in enabled:
            self.assertTrue(a.enabled)

    def test_binance_symbols(self):
        bs = get_all_binance_symbols()
        self.assertIn("BTCUSDT", bs)

    def test_asset_config_fields(self):
        btc = get_asset("BTC/USDT")
        self.assertIn("1h", btc.timeframes)
        self.assertGreater(btc.min_confluence_score, 0)
        self.assertGreater(btc.min_rr, 0)
        self.assertGreater(btc.cooldown_minutes, 0)

    def test_paper_mode_is_bool(self):
        self.assertIsInstance(PAPER_MODE, bool)

    def test_asset_config_dataclass(self):
        a = AssetConfig(
            symbol="TEST/USDT",
            binance_symbol="TESTUSDT",
            timeframes=["1h", "4h"],
            primary_tf="1h",
        )
        self.assertEqual(a.symbol, "TEST/USDT")
        self.assertTrue(a.enabled)  # default

    def test_disabled_asset_excluded(self):
        # Temporarily add a disabled asset and verify it's excluded
        from config.assets import ASSETS
        orig_len = len(ASSETS)
        test_asset = AssetConfig(
            symbol="FAKE/USDT",
            binance_symbol="FAKEUSDT",
            timeframes=["1h"],
            primary_tf="1h",
            enabled=False,
        )
        ASSETS.append(test_asset)
        try:
            enabled = get_enabled_assets()
            enabled_symbols = [a.symbol for a in enabled]
            self.assertNotIn("FAKE/USDT", enabled_symbols)
        finally:
            ASSETS.pop()  # cleanup
            self.assertEqual(len(ASSETS), orig_len)


if __name__ == "__main__":
    unittest.main(verbosity=2)
