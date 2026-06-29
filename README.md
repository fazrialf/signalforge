# SignalForge — AI-Powered Trading Signal System

Automated cryptocurrency trading signal generation using AI reasoning, multi-timeframe SMC/ICT analysis, and Telegram delivery.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    main.py (loop)                     │
│  ┌─────────┐  ┌──────────┐  ┌────────┐  ┌────────┐  │
│  │ Data    │→ │ SMC/ICT  │→ │Conf-   │→ │Filter  │  │
│  │ Feed    │  │ Analysis │  │luence  │  │Gate    │  │
│  └─────────┘  └──────────┘  └────────┘  └────────┘  │
│                                    │                 │
│                                    ▼                 │
│                              ┌──────────┐            │
│                              │LLM       │→ Telegram  │
│                              │Engine    │  Signal    │
│                              └──────────┘            │
│                                    │                 │
│                              ┌──────────┐            │
│                              │Paper     │            │
│                              │Trade     │  $10k sim  │
│                              └──────────┘            │
└─────────────────────────────────────────────────────┘
```

## Features

### Signal Sources (12 categories)
- **SMC/ICT**: BOS, ChOS, FVG, Order Block, Liquidity Grab, Premium/Discount
- **Classic TA**: Support/Resistance, Head & Shoulders, Double Top/Bottom, Wedges, Flags
- **Candlestick**: Engulfing, Pin Bar, Doji, Morning/Evening Star, Inside Bar
- **Momentum**: RSI, MACD, Stochastic, CCI, Williams %R, MFI + divergences
- **Trend**: EMA 20/50/200, VWAP, Supertrend, Ichimoku, ADX
- **Volume**: Volume Profile, OBV, CVD, Climax, Absorption
- **Volatility**: ATR, Bollinger Bands, Keltner Channel, Squeeze
- **Market Structure**: HH/HL/LH/LL, Range, Wyckoff phases
- **On-Chain**: Funding rate, Open Interest, Long/Short ratio, Taker buy/sell
- **Sentiment/Macro**: Fear & Greed, News headlines, Economic calendar
- **Orderbook**: Bid/Ask imbalance, Taker buy/sell ratio
- **Correlation**: BTC dominance, DXY, SPX, Gold

### Confluence Scoring
- **Tier 1 (3x)**: Structure factors (BOS, S/R, Order Block, MTF Bias)
- **Tier 2 (2x)**: Trigger factors (FVG, Liq Grab, Impulsive Candle, Squeeze)
- **Tier 3 (1x)**: Confirmation (RSI, MACD, VWAP, Sentiment)
- **Threshold**: Score ≥ 8 triggers LLM analysis

### Filter Gate (10 filters)
1. Confidence ≥ 75%
2. R:R ≥ 1.5
3. MTF Bias aligned (1D + 4H + 1H)
4. Cooldown (30min)
5. Max 3 concurrent positions
6. Portfolio heat < 6%
7. No high-impact news within 2H
8. Volatility regime OK
9. Fear & Greed not extreme
10. Spread OK

### External Data
- Crypto news (CoinDesk/CoinTelegraph RSS)
- Fear & Greed Index (Alternative.me)
- Economic calendar (CPI/FOMC/NFP)
- On-chain metrics (Binance Futures)
- Correlations (DXY, SPX, Gold, BTC dominance)

### Telegram Commands
- `/status` — Current system health
- `/stats` — Performance statistics
- `/history` — Recent signals
- `/positions` — Open positions
- `/close [id] [price]` — Close a position manually
- `/balance` — Paper trading balance

## Quick Start

### Prerequisites
- Python 3.14
- Binance API keys (optional, for live data)
- Telegram bot token (from @BotFather)

### Installation

```bash
# Clone or copy files to /home/ssm-user/signalforge/
cd /home/ssm-user/signalforge/
pip install -r requirements.txt
cp config/.env.example config/.env
# Edit .env with your API keys
```

### Configuration

Edit `config/.env`:
```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
NINEROUTER_API_KEY=your_9router_key
OPENAI_MODEL=hermes-main
PAPER_TRADING=true  # false for live tracking
```

### Usage

**Development mode:**
```bash
python3 main.py
```

**Production (systemd):**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now signalforge
sudo systemctl status signalforge
```

**Health check:**
```bash
curl http://localhost:8080/health
```

### Logs
```bash
tail -f logs/signalforge.log
```

## Project Structure

```
signalforge/
├── main.py                    # Entry point — async analysis loop
├── config/
│   ├── settings.py            # Configuration & env loading
│   ├── assets.py              # Multi-asset definitions
│   └── .env                   # Secrets (not in repo)
├── core/
│   ├── indicators.py          # 20+ technical indicators
│   ├── swing_points.py        # Swing high/low detection
│   ├── market_structure.py    # HH/HL/LH/LL, BOS/ChOS
│   ├── smc.py                 # S/R, FVG, OB, Liquidity, P/D
│   ├── candlestick.py         # 8 pattern types
│   ├── chart_patterns.py      # 5 chart pattern families
│   ├── divergence.py          # RSI + MACD divergence
│   ├── volume_analysis.py     # RVOL, climax, absorption
│   └── squeeze.py             # BB+KC squeeze
├── signals/
│   ├── pipeline.py            # Full analysis pipeline
│   ├── confluence.py          # Tier-weighted scoring
│   ├── mtf_bias.py            # Multi-timeframe bias
│   ├── prompt_builder.py      # LLM prompt construction
│   ├── llm_engine.py          # Async OpenAI caller
│   ├── filter_gate.py         # 10-filter gate
│   ├── cooldown.py            # Per-asset cooldown
│   ├── risk_sizing.py         # Position sizing
│   ├── signal_log.py          # SQLite signal logging
│   ├── position_tracker.py    # P&L tracking
│   └── win_rate.py            # Performance stats
├── data/
│   ├── fetcher.py             # Historical OHLCV
│   ├── websocket_feed.py      # Live WebSocket stream
│   └── multi_asset_feed.py    # Multi-symbol WS manager
├── external/
│   ├── news_fetcher.py        # Crypto news RSS
│   ├── fear_greed.py          # F&G index
│   ├── economic_calendar.py   # Macro events
│   ├── onchain.py             # Binance Futures metrics
│   └── correlations.py        # DXY, SPX, Gold
├── delivery/
│   ├── telegram_bot.py        # Telegram message sender
│   └── telegram_commands.py   # Bot command handler
├── trading/
│   └── paper_trade.py         # Paper trading engine
├── monitoring/
│   ├── watchdog.py            # Health checks & daily ping
│   ├── progress.py            # Build status tracking
│   ├── weekly_report.py       # Weekly performance report
│   ├── error_alerter.py       # Telegram error alerting
│   ├── health_endpoint.py     # HTTP health check (port 8080)
│   └── stress_test.py         # Historical replay tests
├── db/
│   └── schema.py              # SQLite schema
├── tests/
│   ├── test_sprint2.py        # 37 SMC tests
│   ├── test_sprint3.py        # 44 pattern tests
│   ├── test_sprint4.py        # 30 scoring tests
│   ├── test_sprint5.py        # 33 filter tests
│   ├── test_sprint6.py        # 28 external data tests
│   ├── test_sprint7.py        # 32 position tracking tests
│   ├── test_sprint8.py        # 32 paper trading tests
│   └── test_sprint9.py        # 15 production hardening tests
└── logs/
```

## Sprint Progress

| # | Sprint | Status | Tests |
|---|--------|--------|-------|
| 1 | Foundation (WS, indicators, DB, Telegram) | ✅ | — |
| 2 | SMC Structure Detection | ✅ | 37 |
| 3 | Pattern Detection | ✅ | 44 |
| 4 | Confluence Scoring + LLM | ✅ | 30 |
| 5 | Filter Gate + Signal Delivery | ✅ | 33 |
| 6 | External Data | ✅ | 28 |
| 7 | Position Tracking + Win Rate | ✅ | 32 |
| 8 | Paper Trading + Multi-Asset | ✅ | 32 |
| 9 | Production Hardening | ✅ | 15 |
| **Total** | | **100%** | **≥251** |

## Risk Management

- Max 2% per trade, 3 concurrent positions, 6% portfolio heat
- Min R:R 1.5 — no exceptions
- No entry within 2H of CPI/FOMC/NFP
- 30-min cooldown after signal
- Daily loss limit 4% → auto-pause
- Weekly loss limit 8% → review mode
- Position sizing: 1-2% risk tiered by confidence

## Paper Trading

By default, SignalForge runs in **paper trading mode** (`PAPER_TRADING=true` in `.env`):
- $10,000 simulated balance
- P&L tracked per trade (TP/SL auto-close)
- No real money at risk
- Set `PAPER_TRADING=false` for live position tracking

## Monitoring

- **Health endpoint**: `http://localhost:8080/health`
- **Error alerts**: Telegram notifications on pipeline crashes, LLM failures, WS disconnects
- **Daily summary**: Performance stats every 07:00 WIB
- **Weekly report**: Full performance review every Sunday
- **Log rotation**: Daily, 30-day retention (logrotate configured)

## License

Internal use — SignalForge by Fazrial
