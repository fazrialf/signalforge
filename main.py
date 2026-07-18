"""
SignalForge Main Entry Point
SignalForge: initialises DB, loads data, starts WebSocket, runs watchdog.
"""
import asyncio
import logging
import sys
import os
import time
import sqlite3
from pathlib import Path

import pandas as pd

# Make project root importable
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from config.settings import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_IDS,
    TIMEFRAMES, HISTORY_BARS,
    WATCHDOG_INTERVAL, WS_STALE_SECONDS, DAILY_PING_HOUR_UTC,
    STATUS_PATH, LOG_PATH, DB_PATH
)
from db.schema import init_db
from data.fetcher import DataFetcher
from data.multi_asset_feed import MultiAssetFeed
from delivery.telegram_bot import TelegramBot
from monitoring.watchdog import HealthWatchdog

from signals.pipeline import analyse_all_timeframes
from signals.confluence import score_confluence
from signals.bos_retest import BOSRetestWatcher
from signals.mtf_bias import check_mtf_bias
from signals.prompt_builder import build_prompt
from signals.llm_engine import get_signal
from signals.filter_gate import FilterGate, FilterResult
from signals.cooldown import CooldownTracker
from signals.risk_sizing import calc_position_size
from signals.signal_log import log_signal
from signals.position_tracker import PositionTracker
from delivery.telegram_commands import TelegramCommandHandler
from trading.paper_trade import PaperTradeEngine
from config.assets import PAPER_MODE, get_enabled_assets
from monitoring.error_alerter import ErrorAlerter
from monitoring.health_endpoint import HealthServer
from core.session_marker import (
    get_current_session, get_session_opens_in_window,
    format_session_label, format_session_open_alert,
    calc_asia_range,
)
from core.vwap import calc_vwap, VWAPState
from core.smc import detect_equal_levels, get_swept_reclaimed_levels
from data.order_flow import OrderFlowAccumulator
from strategies.vwap_reversion import evaluate_vwap_reversion
from strategies.sweep_reclaim import evaluate_sweep_reclaim
from strategies.delta_divergence import evaluate_delta_divergence
from strategies.session_breakout import evaluate_session_breakout
from strategies.micro_fvg import evaluate_micro_fvg, detect_micro_fvgs
from config.settings import (
    MIN_CONFLUENCE_SCORE, MIN_LLM_CONFIDENCE, MIN_RR_RATIO,
    ACCOUNT_BALANCE, COOLDOWN_MINUTES, SWING_LOOKBACK,
    DB_PATH, LOG_PATH, BASE_DIR,
    MAX_CONCURRENT, MAX_PORTFOLIO_HEAT_PCT,
)
try:
    from external.news_fetcher import fetch_recent_news, is_high_impact_news, get_news_sentiment
    from external.fear_greed import fetch_fear_greed
    from external.onchain import fetch_onchain_metrics
    from external.liquidation_tracker import fetch_liquidation_levels
    from external.correlations import fetch_correlations
    _EXTERNAL_AVAILABLE = True
except ImportError:
    _EXTERNAL_AVAILABLE = False

# --- Logging -------------------------------------------------------
os.makedirs(Path(LOG_PATH).parent, exist_ok=True)
# FileHandler only — systemd unit already appends stdout to the same
# log file (StandardOutput=append:…/signalforge.log).  Keeping both
# StreamHandler + FileHandler doubles every line.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
    ],
    force=True,
)
logger = logging.getLogger("signalforge")


async def main():
    logger.info("=" * 60)
    logger.info("SignalForge starting up")
    logger.info("=" * 60)

    # 1. Init DB
    logger.info("[1/5] Initialising database...")
    init_db()

    # 2. Init Telegram bot
    logger.info("[2/5] Connecting Telegram bot...")
    bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS or TELEGRAM_CHAT_ID)
    await bot.send(
        "\U0001f7e2 <b>SignalForge is starting up!</b>\n"
        "Loading historical data from Binance..."
    )

    # 3. Fetch historical OHLCV for all enabled assets
    logger.info("[3/5] Fetching historical OHLCV data...")
    enabled_assets = get_enabled_assets()
    enabled_symbols = [a.symbol for a in enabled_assets]
    logger.info("Enabled assets: %s", enabled_symbols)

    # Use MultiAssetFeed for fetching history (concurrent fetches)
    multi_feed = MultiAssetFeed(db_path=DB_PATH, on_tick=lambda sym, price: None)
    asset_configs = [
        {
            "symbol": a.symbol,
            "enabled": True,
            "timeframes": a.timeframes,
            "history_bars": a.lookback_bars,
        }
        for a in enabled_assets
    ]
    history_results = await multi_feed.fetch_history(asset_configs, DB_PATH)

    # Build per-asset DataFetcher cache and latest price dict
    asset_fetchers: dict[str, DataFetcher] = {}
    latest_prices: dict[str, float] = {}
    for a in enabled_assets:
        fetcher = DataFetcher(a.symbol, a.timeframes, a.lookback_bars, use_futures=a.use_futures)
        data = await fetcher.load_all()
        asset_fetchers[a.symbol] = fetcher
        latest_prices[a.symbol] = fetcher.latest_price() or 0.0
        for tf, df in data.items():
            logger.info("  %s %s: %d bars loaded, last close = %.2f",
                        a.symbol, tf, len(df),
                        df["close"].iloc[-1] if not df.empty else 0)

    # Q-6: startup assertion — every configured asset must have a fetcher.
    # A missing key causes a silent None later (line ~479: fetcher.latest_candle()
    # on None crashes mid-session). Better to fail loud at boot.
    missing = [a.symbol for a in enabled_assets if a.symbol not in asset_fetchers]
    if missing:
        msg = f"[STARTUP] asset_fetchers missing keys: {missing} — check assets config"
        logger.critical(msg)
        await bot.send(f"❌ <b>Startup aborted</b>\n{msg}")
        raise RuntimeError(msg)

    summary_lines = [f"\U0001f4ca <b>Historical data loaded for {len(enabled_symbols)} assets!</b>"]
    for sym in enabled_symbols:
        p = latest_prices.get(sym, 0.0)
        summary_lines.append(f"<b>{sym}:</b> ${p:,.2f}")
    summary_lines.append(f"Timeframes: {', '.join(set.union(*[set(a.timeframes) for a in enabled_assets]))}")
    await bot.send("\n".join(summary_lines))

    # 4. Start WebSocket feeds for all enabled assets
    logger.info("[4/5] Starting WebSocket feeds...")

    # --- Strategy Infrastructure (created before WS so on_trade can bind) ---
    # Order flow accumulator for delta divergence strategy (5m bars)
    order_flow = OrderFlowAccumulator(bar_interval_ms=300_000, max_bars=100)

    # Shared on_tick callback — updates latest_prices dict
    def on_tick(symbol: str, price: float) -> None:
        latest_prices[symbol] = price

    # Shared on_trade callback — feeds OrderFlowAccumulator from WS trade stream
    def on_trade(symbol: str, tick: dict) -> None:
        """Route Binance trade ticks into the delta accumulator.

        tick fields from WebSocketFeed:
          price, qty, side ('buy'|'sell'), ts (ms)
        is_buyer_maker = True when side == 'sell' (taker sold into maker bid).
        """
        try:
            price = float(tick.get("price") or 0)
            qty = float(tick.get("qty") or 0)
            if price <= 0 or qty <= 0:
                return
            is_buyer_maker = str(tick.get("side", "")).lower() == "sell"
            ts = int(tick.get("ts") or 0)
            if ts <= 0:
                return
            order_flow.on_trade(symbol, price, qty, is_buyer_maker, ts)
        except Exception as exc:
            logger.debug("[ORDER_FLOW] on_trade error for %s: %s", symbol, exc)

    # Create MultiAssetFeed with price + trade callbacks
    ws_feed = MultiAssetFeed(db_path=DB_PATH, on_tick=on_tick, on_trade=on_trade)

    # Build asset configs for WebSocket
    ws_asset_configs = [
        {
            "symbol": a.symbol,
            "enabled": True,
            "timeframes": a.timeframes,
        }
        for a in enabled_assets
    ]
    await ws_feed.start_all(ws_asset_configs)

    # Wait for first BTC tick (up to 15s) to confirm feeds are live
    for _ in range(30):
        await asyncio.sleep(0.5)
        if latest_prices.get("BTC/USDT", 0.0) > 0:
            break

    btc_price = latest_prices.get("BTC/USDT", 0.0)
    if btc_price > 0:
        logger.info("WebSocket live — BTC/USDT first tick received, price = %.2f", btc_price)
        await bot.send(
            f"\U0001f7e2 <b>WebSocket connected!</b>\n"
            f"Live BTC price: <b>${btc_price:,.2f}</b>\n"
            f"Receiving ticks for {len(enabled_symbols)} assets in real-time."
        )
    else:
        logger.warning("WebSocket: no tick received within 15s")
        await bot.send_health_alert("WebSocket: no tick received within 15s after startup")

    # 5. Start Watchdog — uses BTC/USDT feed state for WS health check
    logger.info("[5/5] Starting health watchdog...")
    btc_ws_state = ws_feed.get_state("BTC/USDT")
    btc_ws_ref = btc_ws_state.ws if btc_ws_state else None
    watchdog = HealthWatchdog(
        bot=bot, ws_feed=btc_ws_ref,
        config={
            "watchdog_interval":    WATCHDOG_INTERVAL,
            "daily_ping_hour_utc":  DAILY_PING_HOUR_UTC,
            "status_path":          STATUS_PATH,
        }
    )
    watchdog_task = asyncio.create_task(watchdog.run())

    # --- SMC Analysis Pipeline ---
    # Run analysis every 60 seconds on latest data
    # M-5: per-symbol broadcast timer (was a single shared value — starved non-BTC assets)
    last_structure_broadcast: dict[str, float] = {}

    # Session marker — tracks last check time for one-shot session open alerts
    import datetime as _dt
    last_session_check: _dt.datetime = _dt.datetime.now(_dt.timezone.utc)

    # S-3: BOS retest watcher — per-symbol state machine, lives for the session
    bos_watcher = BOSRetestWatcher()

    # Fix-3: Consecutive PASS tracker — suppresses LLM calls after 3 identical
    # PASS results for the same symbol. Prevents token waste on choppy markets.
    _pass_tracker: dict[str, int] = {}  # symbol → consecutive PASS count
    _PASS_COOLDOWN_THRESHOLD = 3   # after this many PASS results, suppress
    _PASS_COOLDOWN_CYCLES = 5      # skip this many cycles before retrying

    async def run_smc_pipeline():
        nonlocal last_structure_broadcast, latest_prices, active_positions, last_session_check
        while True:
            try:
                # --- Session open alerts ---
                now_utc = _dt.datetime.now(_dt.timezone.utc)
                opened_sessions = get_session_opens_in_window(last_session_check, now_utc)
                for sess in opened_sessions:
                    logger.info("[SESSION] %s open", sess.name)
                    await bot.send(format_session_open_alert(sess, now_utc))
                last_session_check = now_utc

                # Iterate over every enabled asset
                for asset_idx, a in enumerate(enabled_assets):
                    symbol = a.symbol
                    primary_tf = a.primary_tf
                    logger.info("Analyzing %s...", symbol)

                    # Reload fresh OHLCV from DB for this symbol — one
                    # connection per symbol, closed when the with-block exits.
                    fresh_data = {}
                    # Cap bars loaded per TF — full ASC history was memory bloat.
                    # 1m: ~2h for micro-FVG only; higher TFs: asset lookback_bars.
                    # Keep in sync with data.fetcher.LOOKBACK_1M_BARS (120).
                    _tf_limits = {
                        "1m": 120,
                        "5m": min(int(getattr(a, "lookback_bars", 500) or 500), 500),
                        "15m": min(int(getattr(a, "lookback_bars", 500) or 500), 400),
                        "1h": min(int(getattr(a, "lookback_bars", 500) or 500), 300),
                    }
                    with sqlite3.connect(DB_PATH, timeout=10) as _conn:
                        for tf in a.timeframes:
                            limit = _tf_limits.get(tf, int(getattr(a, "lookback_bars", 500) or 500))
                            rows = _conn.execute(
                                "SELECT ts, open, high, low, close, volume "
                                "FROM candles WHERE timeframe = ? AND symbol = ? "
                                "ORDER BY ts DESC LIMIT ?",
                                (tf, symbol, limit),
                            ).fetchall()
                            rows = list(reversed(rows))  # restore ASC order
                            if rows:
                                df_fresh = pd.DataFrame(
                                    rows, columns=["ts", "open", "high", "low", "close", "volume"]
                                )
                                df_fresh["timestamp"] = pd.to_datetime(df_fresh["ts"], unit="ms")
                                df_fresh = df_fresh.set_index("timestamp")
                                df_fresh.drop(columns=["ts"], inplace=True)
                                fresh_data[tf] = df_fresh
                            else:
                                # Fallback to cached data from startup fetcher
                                cached = asset_fetchers.get(symbol)
                                fresh_data[tf] = cached.get(tf) if cached else pd.DataFrame()

                    price = latest_prices.get(symbol, 0.0)
                    # Skip full SMC on 1m — only raw candles needed for Micro-FVG.
                    results = analyse_all_timeframes(
                        fresh_data, current_price=price, skip_timeframes={"1m"}
                    )

                    # Log a summary of what we found
                    for tf, r in results.items():
                        if r.structure:
                            logger.info("[SMC] %s %s: bias=%s | swings=%d | FVGs=%d | OBs=%d | pools=%d | grab=%s",
                                        symbol, tf, r.structure.bias.value, len(r.swing_points),
                                        len(r.active_fvgs), len(r.active_obs), len(r.liquidity_pools),
                                        "YES" if r.recent_grab else "no")

                    # Broadcast a readable snapshot every ~10 minutes (per asset)
                    now = time.time()
                    if now - last_structure_broadcast.get(symbol, 0) >= 600:
                        last_structure_broadcast[symbol] = now
                        tf_4h = results.get("4h")
                        tf_1h = results.get("1h")
                        current_session = get_current_session()
                        msg_parts = [
                            "🏗️ <b>Structure Snapshot</b>",
                            f"Asset: <b>{symbol}</b>",
                            f"Price: <b>${price:,.2f}</b>",
                            f"Session: {format_session_label(current_session)}",
                        ]
                        for label, r in [("4H", tf_4h), ("1H", tf_1h)]:
                            if r and r.structure:
                                msg_parts.append(
                                    f"{label}: {r.structure.bias.value.upper()} "
                                    f"| {len(r.swing_points)} swings"
                                )
                                if r.active_fvgs:
                                    msg_parts.append(f"  FVGs: {len(r.active_fvgs)} active")
                                if r.active_obs:
                                    msg_parts.append(f"  OBs: {len(r.active_obs)} active")
                                if r.recent_grab:
                                    msg_parts.append(f"  ⚡ Liq Grab @ ${r.recent_grab.price:,.2f}")
                                if r.premium_discount:
                                    msg_parts.append(f"  Zone: {r.premium_discount.zone.upper()}")
                                if r.candlestick_patterns:
                                    top3 = r.candlestick_patterns[:3]
                                    pnames = ", ".join(p.pattern for p in top3)
                                    msg_parts.append(f"  🕯 {pnames}")
                                if r.squeeze_firing:
                                    msg_parts.append(f"  💥 SQUEEZE FIRING ({r.squeeze.momentum_direction})")
                                elif r.squeeze and r.squeeze.in_squeeze:
                                    msg_parts.append(f"  🔴 In Squeeze ({r.squeeze.squeeze_bars} bars)")
                                if r.divergences:
                                    d = r.divergences[0]
                                    msg_parts.append(f"  ↕ Divergence: {d.type} ({d.indicator.upper()})")

                        await bot.send("\n".join(msg_parts))

                    # --- Confluence Scoring + LLM ---
                    mtf_bias = check_mtf_bias(results)
                    primary_r = results.get(primary_tf)
                    if primary_r:
                        # onchain + news_sentiment wired in after external fetch below;
                        # initial confluence scored without them (gate decision uses raw structure)
                        confluence = score_confluence(primary_r, mtf_aligned=mtf_bias.aligned, threshold=MIN_CONFLUENCE_SCORE, mtf_bias=mtf_bias)
                        logger.info(
                            "[CONFLUENCE] %s %s | net=%d | bull=%d | bear=%d | threshold=%s",
                            symbol, confluence.dominant_direction.upper(),
                            confluence.net_score,
                            confluence.bullish_score,
                            confluence.bearish_score,
                            'YES' if confluence.meets_threshold else 'no',
                        )

                        # --- Multi-Strategy Evaluation ---
                        # Run all 5 strategies in parallel with BOS retest.
                        # If any strategy fires, it enriches the LLM prompt.
                        strategy_signals = []
                        primary_df = fresh_data.get(primary_tf)
                        df_1m = fresh_data.get("1m")
                        vwap_state = None  # shared by VWAP + Delta strategies

                        # Strategy 1: VWAP Mean Reversion
                        try:
                            vwap_state = calc_vwap(primary_df, anchor_hour_utc=0)
                            squeeze_firing = primary_r.squeeze_firing if primary_r else False
                            vwap_sig = evaluate_vwap_reversion(
                                primary_df, vwap_state=vwap_state,
                                squeeze_firing=squeeze_firing, atr_spike=False,
                            )
                            if vwap_sig:
                                strategy_signals.append(("VWAP_REVERSION", vwap_sig))
                                logger.info("[STRAT] %s VWAP Reversion: %s conf=%d RR=%.1f",
                                            symbol, vwap_sig.direction, vwap_sig.confidence, vwap_sig.rr_ratio)
                        except Exception as e:
                            vwap_state = None
                            logger.debug("[STRAT] %s VWAP error: %s", symbol, e)

                        # Strategy 2: Sweep + Reclaim
                        try:
                            swings = primary_r.swing_points if primary_r else []
                            sweep_sig = evaluate_sweep_reclaim(
                                primary_df, swings=swings, current_price=price,
                            )
                            if sweep_sig:
                                strategy_signals.append(("SWEEP_RECLAIM", sweep_sig))
                                logger.info("[STRAT] %s Sweep+Reclaim: %s conf=%d RR=%.1f",
                                            symbol, sweep_sig.direction, sweep_sig.confidence, sweep_sig.rr_ratio)
                        except Exception as e:
                            logger.debug("[STRAT] %s Sweep error: %s", symbol, e)

                        # Strategy 3: Delta Divergence
                        try:
                            # Health breadcrumb: prove WS trades are accumulating
                            # (logs even before first 5m bar closes)
                            live = order_flow.get_live_snapshot(symbol)
                            if live["current_trades"] > 0 or live["completed_bars"] > 0:
                                logger.info(
                                    "[ORDER_FLOW] %s trades=%d bars=%d "
                                    "cur_delta=%.2f cum_delta=%.2f buy=%.2f sell=%.2f",
                                    symbol,
                                    live["current_trades"],
                                    live["completed_bars"],
                                    live["current_delta"],
                                    live["cum_delta"],
                                    live["current_buy_vol"],
                                    live["current_sell_vol"],
                                )
                            delta_state = order_flow.get_state(symbol)
                            nearest_sup = None
                            nearest_res = None
                            if primary_r and primary_r.sr_levels:
                                supports = [s for s in primary_r.sr_levels if s.type == 'support' and s.price < price]
                                resists = [s for s in primary_r.sr_levels if s.type == 'resistance' and s.price > price]
                                if supports:
                                    nearest_sup = max(s.price for s in supports)
                                if resists:
                                    nearest_res = min(s.price for s in resists)
                            delta_sig = evaluate_delta_divergence(
                                primary_df, delta_state=delta_state,
                                current_price=price,
                                nearest_support=nearest_sup,
                                nearest_resistance=nearest_res,
                                vwap=vwap_state.vwap if vwap_state else None,
                            )
                            if delta_sig:
                                strategy_signals.append(("DELTA_DIVERGENCE", delta_sig))
                                logger.info("[STRAT] %s Delta Divergence: %s conf=%d RR=%.1f",
                                            symbol, delta_sig.direction, delta_sig.confidence, delta_sig.rr_ratio)
                        except Exception as e:
                            logger.debug("[STRAT] %s Delta error: %s", symbol, e)

                        # Strategy 4: Session Breakout
                        try:
                            asia_range = calc_asia_range(primary_df, utc_now=now_utc)
                            session_sig = evaluate_session_breakout(
                                primary_df, symbol=symbol,
                                current_price=price, asia_range=asia_range,
                                utc_now=now_utc,
                            )
                            if session_sig:
                                strategy_signals.append(("SESSION_BREAKOUT", session_sig))
                                logger.info("[STRAT] %s Session Breakout: %s conf=%d RR=%.1f (%s open)",
                                            symbol, session_sig.direction, session_sig.confidence,
                                            session_sig.rr_ratio, session_sig.session_trigger)
                        except Exception as e:
                            logger.debug("[STRAT] %s Session error: %s", symbol, e)

                        # Strategy 5: Micro-FVG Stacking (refinement only — requires parent zone)
                        try:
                            if df_1m is not None and len(df_1m) > 20 and primary_r:
                                # Use nearest 5m FVG as parent zone — REQUIRED
                                parent_type = "FVG"
                                parent_top = None
                                parent_bottom = None
                                if primary_r.active_fvgs:
                                    nearest_fvg = min(primary_r.active_fvgs,
                                                     key=lambda f: abs(f.midpoint - price))
                                    parent_top = nearest_fvg.top
                                    parent_bottom = nearest_fvg.bottom
                                elif primary_r.active_obs:
                                    nearest_ob = min(primary_r.active_obs,
                                                    key=lambda o: abs(o.midpoint - price))
                                    parent_top = nearest_ob.top
                                    parent_bottom = nearest_ob.bottom
                                    parent_type = "OB"

                                # Skip Micro-FVG entirely if no parent zone nearby
                                if parent_top is not None and parent_bottom is not None:
                                    structure_bias = None
                                    if primary_r.structure and primary_r.structure.bias:
                                        structure_bias = getattr(
                                            primary_r.structure.bias, "value",
                                            str(primary_r.structure.bias),
                                        )
                                    micro_sig = evaluate_micro_fvg(
                                        primary_df, df_1m, current_price=price,
                                        parent_zone_type=parent_type,
                                        parent_zone_top=parent_top,
                                        parent_zone_bottom=parent_bottom,
                                        require_parent_zone=True,
                                        require_price_in_zone=True,
                                        structure_bias=structure_bias,
                                    )
                                    if micro_sig:
                                        strategy_signals.append(("MICRO_FVG", micro_sig))
                                        logger.info("[STRAT] %s Micro-FVG: %s conf=%d RR=%.1f (%d FVGs stacked)",
                                                    symbol, micro_sig.direction, micro_sig.confidence,
                                                    micro_sig.rr_ratio, micro_sig.fvg_count)
                        except Exception as e:
                            logger.debug("[STRAT] %s Micro-FVG error: %s", symbol, e)

                        # Log strategy summary
                        if strategy_signals:
                            logger.info("[STRAT] %s — %d strategy signal(s) active: %s",
                                        symbol, len(strategy_signals),
                                        ", ".join(s[0] for s in strategy_signals))

                        # --- Decision: BOS retest OR strategy signal triggers LLM ---
                        should_call_llm = False
                        llm_trigger_reason = ""

                        if confluence.meets_threshold:
                            # Fix-3: Skip LLM if this symbol has been returning PASS repeatedly
                            pass_count = _pass_tracker.get(symbol, 0)
                            if pass_count >= _PASS_COOLDOWN_THRESHOLD:
                                # Decrement counter each skipped cycle; when it reaches 0, retry
                                _pass_tracker[symbol] = pass_count - 1
                                logger.info(
                                    "[PASS_COOLDOWN] %s skipping LLM — %d consecutive PASS results "
                                    "(retry in %d cycles)",
                                    symbol, _PASS_COOLDOWN_THRESHOLD, pass_count - 1,
                                )
                            else:
                                # S-3: BOS retest gate — suppress LLM until price
                                # pulls back into the FVG/OB left by the BOS impulse
                                retest_ok, retest_reason = bos_watcher.update(
                                    symbol=symbol,
                                    primary_r=primary_r,
                                    current_price=price,
                                )
                                if retest_ok:
                                    should_call_llm = True
                                    llm_trigger_reason = "confluence+bos_retest"
                                else:
                                    logger.info(
                                        "[BOS_RETEST] %s LLM suppressed — %s",
                                        symbol, retest_reason,
                                    )

                        # Strategy signals can independently trigger LLM, but with
                        # per-strategy confidence floors (Sprint 13 — stop Micro-FVG spam).
                        # MICRO_FVG is refinement-only: needs higher conf OR confluence support.
                        _STRAT_LLM_MIN_CONF = {
                            "MICRO_FVG": 80,
                            "VWAP_REVERSION": 70,
                            "SWEEP_RECLAIM": 65,
                            "SESSION_BREAKOUT": 65,
                            "DELTA_DIVERGENCE": 70,
                        }
                        if not should_call_llm and strategy_signals:
                            # Prefer non-micro strategies when available
                            ranked = sorted(
                                strategy_signals,
                                key=lambda s: (
                                    0 if s[0] != "MICRO_FVG" else 1,
                                    -s[1].confidence,
                                ),
                            )
                            best_strat = ranked[0]
                            sname, ssig = best_strat
                            min_conf = _STRAT_LLM_MIN_CONF.get(sname, 70)

                            # Micro-FVG may also trigger at 70+ IF confluence already
                            # supports the same direction (net score ≥ 4).
                            micro_boost_ok = False
                            if sname == "MICRO_FVG" and confluence is not None:
                                same_dir = (
                                    (ssig.direction == "BUY" and confluence.net_score >= 4)
                                    or (ssig.direction == "SELL" and confluence.net_score <= -4)
                                )
                                micro_boost_ok = same_dir and ssig.confidence >= 70

                            if ssig.confidence >= min_conf or micro_boost_ok:
                                # Strategy-level PASS cooldown still applies
                                pass_count = _pass_tracker.get(symbol, 0)
                                if pass_count >= _PASS_COOLDOWN_THRESHOLD:
                                    _pass_tracker[symbol] = pass_count - 1
                                    logger.info(
                                        "[PASS_COOLDOWN] %s skipping strategy LLM (%s) — "
                                        "cooldown active (retry in %d)",
                                        symbol, sname, pass_count - 1,
                                    )
                                else:
                                    should_call_llm = True
                                    boost_tag = "+confluence_boost" if (
                                        micro_boost_ok and ssig.confidence < min_conf
                                    ) else ""
                                    llm_trigger_reason = f"strategy:{sname}{boost_tag}"
                                    logger.info(
                                        "[STRAT] %s triggering LLM via %s (conf=%d, min=%d%s)",
                                        symbol, sname, ssig.confidence, min_conf,
                                        ", confluence_boost" if boost_tag else "",
                                    )
                            else:
                                logger.info(
                                    "[STRAT] %s %s conf=%d below min=%d — no LLM",
                                    symbol, sname, ssig.confidence, min_conf,
                                )

                        if not should_call_llm:
                            if not confluence.meets_threshold:
                                logger.debug(
                                    "[CONFLUENCE] %s Score %d below threshold %d — skipping LLM call",
                                    symbol, abs(confluence.net_score), MIN_CONFLUENCE_SCORE,
                                )
                            continue

                        if should_call_llm:

                            logger.info("[LLM] %s triggering LLM — reason: %s", symbol, llm_trigger_reason)

                            # --- Fetch external data ---
                            external_data = {}
                            if _EXTERNAL_AVAILABLE:
                                try:
                                    external_data['fear_greed'] = await asyncio.to_thread(fetch_fear_greed)  # type: ignore[name-defined]  # guarded by _EXTERNAL_AVAILABLE
                                except Exception as e:
                                    logger.warning("[EXT] Fear & Greed fetch failed: %s", e)
                                try:
                                    external_data['onchain'] = await asyncio.to_thread(fetch_onchain_metrics)
                                except Exception as e:
                                    logger.warning("[EXT] On-chain fetch failed: %s", e)
                                try:
                                    liq_data = fetch_liquidation_levels(symbol, price)
                                    external_data['liquidation'] = liq_data
                                    logger.info(
                                        "[EXT] %s liq clusters=%d dense=%s sweep=%s",
                                        symbol,
                                        len(liq_data.get('clusters', [])),
                                        liq_data.get('dense_cluster_nearby', False),
                                        liq_data.get('sweep_direction'),
                                    )
                                except Exception as e:
                                    logger.warning("[EXT] Liquidation fetch failed for %s: %s", symbol, e)
                                try:
                                    external_data['correlations'] = await asyncio.to_thread(fetch_correlations)
                                except Exception as e:
                                    logger.warning("[EXT] Correlations fetch failed: %s", e)
                                try:
                                    news = fetch_recent_news(hours=2, keywords=['Bitcoin', 'BTC', 'Fed', 'FOMC', 'SEC', 'regulation'])
                                    external_data['news'] = [a for a in news if is_high_impact_news(a)]
                                    # Derive sentiment string for confluence + signal formatting
                                    external_data['news_sentiment'] = get_news_sentiment(symbol, max_age_minutes=120)
                                    logger.info("[EXT] News sentiment for %s: %s", symbol, external_data['news_sentiment'])
                                except Exception as e:
                                    logger.warning("[EXT] News fetch failed: %s", e)

                            # Build strategy context string for LLM prompt
                            strat_context = ""
                            if strategy_signals:
                                strat_lines = ["\n--- ACTIVE STRATEGY SIGNALS ---"]
                                for sname, ssig in strategy_signals:
                                    strat_lines.append(
                                        f"[{sname}] {ssig.direction} | Entry=${ssig.entry:.4f} | "
                                        f"SL=${ssig.stop_loss:.4f} | TP1=${ssig.tp1:.4f} | "
                                        f"R:R={ssig.rr_ratio:.1f} | Confidence={ssig.confidence}%"
                                    )
                                    strat_lines.append(f"  Reasoning: {ssig.reasoning}")
                                strat_context = "\n".join(strat_lines)
                                external_data['strategy_signals'] = strat_context

                            # Re-score confluence with onchain + news enrichment
                            onchain_data = external_data.get('onchain') if _EXTERNAL_AVAILABLE else None
                            news_sent = external_data.get('news_sentiment', 'neutral') if _EXTERNAL_AVAILABLE else 'neutral'
                            if onchain_data or news_sent != 'neutral':
                                confluence = score_confluence(
                                    primary_r, mtf_aligned=mtf_bias.aligned,
                                    threshold=MIN_CONFLUENCE_SCORE, mtf_bias=mtf_bias,
                                    onchain=onchain_data, news_sentiment=news_sent,
                                    liquidation=external_data.get('liquidation'),
                                )
                                logger.info(
                                    "[CONFLUENCE+EXT] %s enriched: net=%d bull=%d bear=%d funding=%s oi_chg=%.1f%% news=%s",
                                    symbol, confluence.net_score, confluence.bullish_score, confluence.bearish_score,
                                    onchain_data.get('funding_sentiment', 'n/a') if onchain_data else 'n/a',
                                    onchain_data.get('oi_change_pct', 0.0) if onchain_data else 0.0,
                                    news_sent,
                                )

                            sys_prompt, usr_prompt = build_prompt(
                                results=results,
                                confluence=confluence,
                                mtf_bias=mtf_bias,
                                symbol=symbol,
                                current_price=price,
                                primary_tf=primary_tf,
                                candle_df=primary_df,
                                external_data=external_data,
                            )
                            signal = await get_signal(
                                sys_prompt, usr_prompt,
                                min_confidence=MIN_LLM_CONFIDENCE,
                            )
                            logger.info(
                                "[LLM] %s Signal=%s | Confidence=%.0f%% | R:R=%.1f | Model=%s",
                                symbol, signal.signal, signal.confidence, signal.rr_ratio, signal.model_used,
                            )
                            health_server.set_health("llm", "ok", f"Model: {signal.model_used}")

                            # --- Filter Gate ---
                            if signal.signal != "PASS":
                                # Fix-3: Reset PASS counter on actionable signal
                                _pass_tracker.pop(symbol, None)
                                # Inject pre-fetched Fear & Greed so filter 9
                                # doesn't make a blocking sync HTTP call
                                if _EXTERNAL_AVAILABLE and 'fear_greed' in external_data:
                                    filter_gate.set_fear_greed(external_data['fear_greed'])
                                filter_result = filter_gate.apply(
                                    signal=signal,
                                    mtf_bias=mtf_bias,
                                    symbol=symbol,
                                    active_positions=active_positions,
                                    current_price=price,
                                    candles=primary_df,
                                )
                                filter_str = "delivered" if filter_result.passed else f"filtered: {filter_result.reason}"
                                logger.info(
                                    "[FILTER] %s %s — %s",
                                    "PASS" if filter_result.passed else "BLOCK",
                                    symbol, filter_str,
                                )

                                # M-2: log_signal called once only — after filter decision
                                if filter_result.passed:
                                    # C-3: guard against entry==stop_loss crash
                                    if not signal.is_actionable:
                                        logger.warning("[RISK] Signal not actionable — skipping position sizing")
                                        continue
                                    # Calculate position size before delivery
                                    pos_size = calc_position_size(
                                        account_balance=ACCOUNT_BALANCE,
                                        confidence=signal.confidence,
                                        entry=signal.entry,
                                        stop_loss=signal.stop_loss,
                                        side="LONG" if signal.signal == "BUY" else "SHORT",
                                        symbol=symbol,
                                    )
                                    # Build enriched message with position sizing
                                    # Build percentage offsets for TP/SL display
                                    def _pct(target, base, direction):
                                        if base == 0:
                                            return 0.0
                                        raw = (target - base) / base * 100
                                        return raw if direction == "BUY" else -raw
                                    _dir = signal.signal
                                    _e = signal.entry
                                    # Confluence factor breakdown for display
                                    _cf_detail = {}
                                    for f in confluence.factors:
                                        tier_key = f"tier{f.tier}"
                                        _cf_detail.setdefault(tier_key, [])
                                        _cf_detail[tier_key].append(f.description)
                                    signal_dict = {
                                        "symbol":          symbol,
                                        "direction":       _dir,
                                        "entry_price":     _e,
                                        "tp1":             signal.tp1,
                                        "tp2":             signal.tp2,
                                        "tp3":             signal.tp3,
                                        "sl":              signal.stop_loss,
                                        "tp1_pct":         _pct(signal.tp1, _e, _dir),
                                        "tp2_pct":         _pct(signal.tp2, _e, _dir),
                                        "tp3_pct":         _pct(signal.tp3, _e, _dir),
                                        "sl_pct":          _pct(signal.stop_loss, _e, _dir),
                                        "rr_ratio":        signal.rr_ratio,
                                        "confidence":      int(signal.confidence),
                                        "risk_pct":        pos_size.risk_pct,
                                        "confluence_score": confluence.net_score,
                                        "confluence_detail": _cf_detail,
                                        "bias_tf":         "15m",
                                        "entry_tf":        primary_tf,
                                        "expiry_hours":    4,
                                        "reasoning":       signal.reasoning,
                                        "primary_risk":    signal.key_risk,
                                        "onchain":         external_data.get('onchain', {}),
                                        "news_sentiment":  external_data.get('news_sentiment', 'neutral'),
                                        "liquidation":     external_data.get('liquidation', {}),
                                    }
                                    await bot.send_signal(signal_dict)
                                    # Also send position sizing as follow-up line
                                    await bot.send(
                                        f"\U0001f4cf <b>Position Sizing</b>\n"
                                        f"Risk: {pos_size.risk_pct:.1f}% (${pos_size.risk_usd:,.0f})\n"
                                        f"Size: {pos_size.size:.4f}  |  Notional: ${pos_size.notional_usd:,.0f}"
                                    )
                                    # Set cooldown + track position
                                    cooldown_tracker.set_cooldown(symbol)
                                    # S-3: reset BOS retest watcher after delivery
                                    bos_watcher.reset(symbol)

                                    # --- Track position in DB ---
                                    try:
                                        sig_id = log_signal(
                                            signal=signal,
                                            symbol=symbol,
                                            confluence_score=confluence.net_score,
                                            mtf_aligned=mtf_bias.aligned,
                                            filter_result="delivered",
                                            cooldown_remaining=0,
                                            db_path=str(DB_PATH),
                                        )
                                        # --- Paper or Live Position ---
                                        if PAPER_MODE and paper_engine is not None:
                                            paper_id = paper_engine.open_trade(
                                                symbol=symbol,
                                                direction="LONG" if signal.signal == "BUY" else "SHORT",
                                                entry=signal.entry,
                                                sl=signal.stop_loss,
                                                tp1=signal.tp1,
                                                tp2=signal.tp2,
                                                tp3=signal.tp3,
                                                size=pos_size.size,
                                                signal_id=sig_id,
                                                confidence=signal.confidence,
                                            )
                                            logger.info("[PAPER] Opened paper trade #%d for %s (balance: $%.2f)", paper_id, symbol, paper_engine.get_balance())
                                        else:
                                            position_id = position_tracker.open_position(
                                                signal_id=sig_id,
                                                symbol=symbol,
                                                direction="LONG" if signal.signal == "BUY" else "SHORT",
                                                entry_price=signal.entry,
                                                position_size=pos_size.size,
                                                stop_loss=signal.stop_loss,
                                                tp1=signal.tp1,
                                                tp2=signal.tp2,
                                                tp3=signal.tp3,
                                            )
                                            logger.info("[POSITION] Opened live position #%d for %s in DB", position_id, symbol)
                                    except Exception as pos_err:
                                        logger.warning("[POSITION] Failed to track position: %s", pos_err)

                                    active_positions.append({
                                        "symbol": symbol,
                                        "side": "LONG" if signal.signal == "BUY" else "SHORT",
                                        "entry": signal.entry,
                                        "sl": signal.stop_loss,
                                        "risk_pct": pos_size.risk_pct,
                                    })
                                    logger.info(
                                        "[DELIVERY] %s signal delivered. Cooldown set 30min. "
                                        "Active positions: %d", symbol, len(active_positions)
                                    )
                                else:
                                    logger.info(
                                        "[FILTER] %s signal blocked: %s",
                                        symbol, filter_result.reason,
                                    )
                            else:
                                # PASS signal — still log it
                                # Fix-3: Increment consecutive PASS counter
                                _pass_tracker[symbol] = _pass_tracker.get(symbol, 0) + 1
                                log_signal(
                                    signal=signal,
                                    symbol=symbol,
                                    confluence_score=confluence.net_score,
                                    mtf_aligned=mtf_bias.aligned,
                                    filter_result="pass",
                                    db_path=str(DB_PATH),
                                )
                                if not signal.error:
                                    logger.info("[LLM] %s LLM returned PASS: %s", symbol, signal.reasoning[:100])

                    # Rate limit between assets to avoid LLM API burst
                    if asset_idx < len(enabled_assets) - 1:
                        await asyncio.sleep(3)

                    # Fix-6: Explicitly release per-asset DataFrames to reduce memory pressure
                    del fresh_data

                # --- GAP FILL: Paper Engine Auto-Tick ---
                for asset in enabled_assets:
                    try:
                        if paper_engine is None:
                            break
                        # M-9: pass candle high/low for wick-accurate SL/TP evaluation
                        fetcher = asset_fetchers.get(asset.symbol)
                        candle = fetcher.latest_candle() if fetcher else None
                        _tick_price = latest_prices.get(asset.symbol, 0)
                        if _tick_price == 0:
                            logger.debug("[PAPER] %s skipping tick — price is 0", asset.symbol)
                            continue
                        ticks = paper_engine.tick(
                            asset.symbol,
                            _tick_price,
                            candle_high=candle["high"] if candle else None,
                            candle_low=candle["low"]  if candle else None,
                        )
                        if ticks:
                            for t in ticks:
                                reason = t.get("reason", "?")
                                pnl = t.get("pnl_usd", 0)
                                logger.info("[PAPER] %s auto-closed: %s P&L=$%.2f", asset.symbol, reason, pnl)
                    except Exception as tick_err:
                        logger.warning("[PAPER] tick error for %s: %s", asset, tick_err)

                # Source-of-truth rebuild after paper ticks (prevents ghost 3/3 blocks)
                active_positions = _sync_active_positions_from_db()
                logger.info(
                    "[POSITION_SYNC] active=%d symbols=%s",
                    len(active_positions),
                    [p["symbol"] for p in active_positions],
                )

                logger.info("SMC analysis cycle complete for all %d assets.", len(enabled_assets))
                health_server.set_health("pipeline", "ok", f"Cycle complete, {len(enabled_assets)} assets analyzed")
            except Exception as e:
                logger.error("SMC pipeline error: %s", e, exc_info=True)
                alerter.report_error("pipeline", str(e), "pipeline_crash")
                health_server.set_health("pipeline", "degraded", str(e)[:100])

            await asyncio.sleep(60)

    # --- Filter Gate + Risk Sizing ---
    # M-1: was hardcoded 30 — now reads COOLDOWN_MINUTES from settings
    cooldown_tracker = CooldownTracker(
        default_cooldown_minutes=COOLDOWN_MINUTES,
        db_path=str(DB_PATH),
    )
    filter_gate = FilterGate(
        cooldown_tracker=cooldown_tracker,
        db_path=str(DB_PATH),
        config={
            'min_confidence': MIN_LLM_CONFIDENCE,
            'min_rr': MIN_RR_RATIO,
            'max_active_signals': MAX_CONCURRENT,
            'max_heat': MAX_PORTFOLIO_HEAT_PCT,
        },
    )
    active_positions: list[dict] = []   # rebuilt from DB each cycle (source of truth)

    def _sync_active_positions_from_db() -> list[dict]:
        """Rebuild active_positions from OPEN paper_trades.

        DB is source of truth — in-memory append/prune can drift after restarts,
        duplicate opens, or closes that miss the paper tick path.
        Dedupes by symbol (keeps newest open row) so concurrent-count is correct.
        """
        synced: list[dict] = []
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as _rc:
                _rc.row_factory = sqlite3.Row
                _open_rows = _rc.execute(
                    "SELECT symbol, direction, entry_price, sl, position_size, opened_at "
                    "FROM paper_trades WHERE status = 'OPEN' "
                    "ORDER BY opened_at DESC"
                ).fetchall()
            seen: set[str] = set()
            for _row in _open_rows:
                sym = _row["symbol"]
                if sym in seen:
                    continue  # keep newest open only per symbol
                seen.add(sym)
                _sl_dist = abs(float(_row["entry_price"]) - float(_row["sl"]))
                _size = float(_row["position_size"] or 0.0)
                _risk_usd = _sl_dist * _size
                _risk_pct = (_risk_usd / ACCOUNT_BALANCE * 100.0) if ACCOUNT_BALANCE else 0.0
                synced.append({
                    "symbol":    sym,
                    "side":      _row["direction"],
                    "entry":     float(_row["entry_price"]),
                    "stop_loss": float(_row["sl"]),
                    "sl":        float(_row["sl"]),
                    "risk_usd":  _risk_usd,
                    "risk_pct":  _risk_pct,
                })
        except Exception as _e:
            logger.warning("[POSITION_SYNC] DB sync failed, keeping previous list: %s", _e)
            return list(active_positions)
        return synced

    # M-3: Restore open positions from DB on startup
    active_positions = _sync_active_positions_from_db()
    if active_positions:
        logger.info(
            "[M-3] Restored %d open paper position(s) from DB: %s",
            len(active_positions),
            [p["symbol"] for p in active_positions],
        )

    # --- Position Tracker + Command Handler ---
    position_tracker = PositionTracker(db_path=str(DB_PATH))
    cmd_handler = TelegramCommandHandler(
        db_path=str(DB_PATH),
        position_tracker=position_tracker,
        bot=bot,
    )

    # --- Paper Trading Engine ---
    # Q-5: only instantiate when PAPER_MODE is enabled — avoids DB schema init in live mode
    if PAPER_MODE:
        paper_engine = PaperTradeEngine(db_path=str(DB_PATH), initial_balance=ACCOUNT_BALANCE)
        logger.info("PAPER MODE enabled — signals simulated, no real orders")
    else:
        paper_engine = None

    # --- Error Alerter + Health Endpoint ---
    alerter = ErrorAlerter(
        bot=bot,
        chat_id=TELEGRAM_CHAT_ID,
        log_path=LOG_PATH,
        check_interval=60,
    )
    health_server = HealthServer(port=8080, data_dir=str(BASE_DIR))
    
    # Start health server and error monitoring
    health_task = asyncio.create_task(health_server.start())
    monitor_task = asyncio.create_task(alerter.start_monitoring())
    health_server.set_health('database', 'ok', 'SQLite connected')
    # Register WebSocket health (BTC price already available)
    for sym in enabled_symbols:
        is_alive = latest_prices.get(sym, 0.0) > 0
        health_server.set_ws_symbol(sym, is_alive)
    has_ticks = any(latest_prices.get(s, 0.0) > 0 for s in enabled_symbols)
    if has_ticks:
        health_server.set_health("websocket", "ok", f"Live, {len(enabled_symbols)} assets")
    else:
        health_server.set_health("websocket", "degraded", "no BTC tick yet")
    logger.info("Error alerter + health endpoint started (port 8080)")

    smc_task = asyncio.create_task(run_smc_pipeline())

    await bot.send("🟢 <b>SignalForge is online</b>\n\nSMC analysis running every 60s.")

    logger.info("SignalForge running. SMC analysis every 60s.")

    # --- GAP FILL: Telegram Command Polling ---
    polling_task = asyncio.create_task(bot.start_polling(cmd_handler, poll_interval=1.0), name="telegram-polling")

    # Keep running (WebSocket + watchdog + SMC run forever)
    try:
        await asyncio.gather(watchdog_task, smc_task, polling_task, health_task, monitor_task)
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        await ws_feed.stop_all()
        for fetcher in asset_fetchers.values():
            await fetcher.close()
        logger.info("SignalForge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
