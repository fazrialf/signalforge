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
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
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
from config.settings import MIN_CONFLUENCE_SCORE, MIN_LLM_CONFIDENCE, MIN_RR_RATIO, ACCOUNT_BALANCE, COOLDOWN_MINUTES, SWING_LOOKBACK, DB_PATH, LOG_PATH, BASE_DIR
try:
    from external.news_fetcher import fetch_recent_news, is_high_impact_news
    from external.fear_greed import fetch_fear_greed
    from external.onchain import fetch_onchain_metrics
    from external.correlations import fetch_correlations
    _EXTERNAL_AVAILABLE = True
except ImportError:
    _EXTERNAL_AVAILABLE = False

# --- Logging -------------------------------------------------------
os.makedirs(Path(LOG_PATH).parent, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
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
    bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
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
        fetcher = DataFetcher(a.symbol, a.timeframes, a.lookback_bars)
        data = await fetcher.load_all()
        asset_fetchers[a.symbol] = fetcher
        latest_prices[a.symbol] = fetcher.latest_price() or 0.0
        for tf, df in data.items():
            logger.info("  %s %s: %d bars loaded, last close = %.2f",
                        a.symbol, tf, len(df),
                        df["close"].iloc[-1] if not df.empty else 0)

    summary_lines = [f"\U0001f4ca <b>Historical data loaded for {len(enabled_symbols)} assets!</b>"]
    for sym in enabled_symbols:
        p = latest_prices.get(sym, 0.0)
        summary_lines.append(f"<b>{sym}:</b> ${p:,.2f}")
    summary_lines.append(f"Timeframes: {', '.join(set.union(*[set(a.timeframes) for a in enabled_assets]))}")
    await bot.send("\n".join(summary_lines))

    # 4. Start WebSocket feeds for all enabled assets
    logger.info("[4/5] Starting WebSocket feeds...")

    # Shared on_tick callback — updates latest_prices dict
    def on_tick(symbol: str, price: float) -> None:
        latest_prices[symbol] = price

    # Create MultiAssetFeed with the shared callback
    ws_feed = MultiAssetFeed(db_path=DB_PATH, on_tick=on_tick)

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

    async def run_smc_pipeline():
        nonlocal last_structure_broadcast, latest_prices, active_positions
        while True:
            try:
                # Iterate over every enabled asset
                for asset_idx, a in enumerate(enabled_assets):
                    symbol = a.symbol
                    primary_tf = a.primary_tf
                    logger.info("Analyzing %s...", symbol)

                    # Reload fresh OHLCV from DB for this symbol
                    fresh_data = {}
                    for tf in a.timeframes:
                        rows = conn.execute(
                            f"SELECT ts, open, high, low, close, volume "
                            f"FROM candles WHERE timeframe = ? AND symbol = ? "
                            f"ORDER BY ts ASC", (tf, symbol)
                        ).fetchall()
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
                    results = analyse_all_timeframes(fresh_data, current_price=price)

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
                        msg_parts = [
                            "🏗️ <b>Structure Snapshot</b>",
                            f"Asset: <b>{symbol}</b>",
                            f"Price: <b>${price:,.2f}</b>",
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
                        confluence = score_confluence(primary_r, mtf_aligned=mtf_bias.aligned, threshold=MIN_CONFLUENCE_SCORE)
                        logger.info(
                            "[CONFLUENCE] %s %s | net=%d | bull=%d | bear=%d | threshold=%s",
                            symbol, confluence.dominant_direction.upper(),
                            confluence.net_score,
                            confluence.bullish_score,
                            confluence.bearish_score,
                            'YES' if confluence.meets_threshold else 'no',
                        )

                        if confluence.meets_threshold:
                            logger.info("[LLM] %s confluence threshold met — calling GPT-4o...", symbol)

                            # --- Fetch external data ---
                            external_data = {}
                            if _EXTERNAL_AVAILABLE:
                                try:
                                    external_data['fear_greed'] = await asyncio.to_thread(fetch_fear_greed)  # type: ignore[name-defined]  # guarded by _EXTERNAL_AVAILABLE
                                except Exception as e:
                                    logger.warning("[EXT] Fear & Greed fetch failed: %s", e)
                                try:
                                    external_data['onchain'] = fetch_onchain_metrics()
                                except Exception as e:
                                    logger.warning("[EXT] On-chain fetch failed: %s", e)
                                try:
                                    external_data['correlations'] = fetch_correlations()
                                except Exception as e:
                                    logger.warning("[EXT] Correlations fetch failed: %s", e)
                                try:
                                    news = fetch_recent_news(hours=2, keywords=['Bitcoin', 'BTC', 'Fed', 'FOMC', 'SEC', 'regulation'])
                                    external_data['news'] = [a for a in news if is_high_impact_news(a)]
                                except Exception as e:
                                    logger.warning("[EXT] News fetch failed: %s", e)

                            primary_df = fresh_data.get(primary_tf)
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
                                    msg = signal.to_telegram_message(symbol)
                                    msg += (
                                        f"\n\n\U0001f4cf <b>Position Sizing</b>"
                                        f"\nRisk: {pos_size.risk_pct:.1f}% "
                                        f"(${pos_size.risk_usd:,.0f})"
                                        f"\nSize: {pos_size.size:.4f}"
                                        f"\nNotional: ${pos_size.notional_usd:,.0f}"
                                    )
                                    await bot.send(msg)
                                    # Set cooldown + track position
                                    cooldown_tracker.set_cooldown(symbol)

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
                        else:
                            logger.debug(
                                "[CONFLUENCE] %s Score %d below threshold %d — skipping LLM call",
                                symbol, abs(confluence.net_score), MIN_CONFLUENCE_SCORE,
                            )

                    # Rate limit between assets to avoid LLM API burst
                    if asset_idx < len(enabled_assets) - 1:
                        await asyncio.sleep(3)

                # --- GAP FILL: Paper Engine Auto-Tick ---
                # C-5: prune active_positions for any symbols the paper engine just closed
                closed_symbols: set[str] = set()
                for asset in enabled_assets:
                    try:
                        if paper_engine is None:
                            break
                        ticks = paper_engine.tick(asset.symbol, latest_prices.get(asset.symbol, 0))
                        if ticks:
                            for t in ticks:
                                reason = t.get("reason", "?")
                                pnl = t.get("pnl_usd", 0)
                                logger.info("[PAPER] %s auto-closed: %s P&L=$%.2f", asset.symbol, reason, pnl)
                                closed_symbols.add(asset.symbol)
                    except Exception as tick_err:
                        logger.warning("[PAPER] tick error for %s: %s", asset, tick_err)
                # Remove closed symbols from active_positions so FilterGate heat check stays accurate
                if closed_symbols:
                    active_positions = [p for p in active_positions if p.get("symbol") not in closed_symbols]
                    logger.debug("[C-5] Pruned active_positions — removed %d closed: %s", len(closed_symbols), closed_symbols)

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
        config={
            'min_confidence': MIN_LLM_CONFIDENCE,
            'min_rr': MIN_RR_RATIO,
            'max_active_signals': 3,
            'max_heat': 6.0,
        },
    )
    active_positions: list[dict] = []   # C-5: pruned on each cycle — entries removed when paper engine closes them

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
        paper_engine = PaperTradeEngine(db_path=str(DB_PATH), initial_balance=10000.0)
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
    health_server.set_health('database', 'healthy', 'SQLite connected')
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

    conn = sqlite3.connect(DB_PATH)
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
