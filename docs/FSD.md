# Functional Specification Document
## SignalForge — AI-Powered Trading Signal System

**Version:** 1.0 | **Classification:** Confidential  
**Date:** 25 June 2026  
**Prepared by:** Hermes Agent (AI Assistant)  
**Status:** Final

---

## Document Control

| Version | Date | Author | Description |
|---------|------|--------|-------------|
| 1.0 | 25 Jun 2026 | Hermes Agent | Initial FSD — complete functional specification |

---

## Table of Contents

1. System Overview
2. Module Specifications — Layer 0 to Layer 12
3. Data Flow Architecture
4. API Specifications
5. Database Schema
6. Error Handling & Edge Cases
7. Performance Requirements
8. Configuration Reference
9. Approval

---

## 1. System Overview

SignalForge is a single-process asynchronous Python application that runs continuously on a Linux server (AWS EC2). It connects to Binance via WebSocket for live price data, processes market data through a 12-layer analysis pipeline, and delivers high-confidence trading signals to the user via a Telegram bot.

### 1.1 Architectural Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     SignalForge Pipeline                      │
│                                                               │
│  Binance WS ──→ Layer 0: Data Ingestion                       │
│                     ↓                                          │
│                  Layer 1: Feature Engineering                  │
│                     ↓                                          │
│                  Layer 2: Pattern & Structure Detection        │
│                     ↓                                          │
│                  Layer 3: Confluence Scoring                   │
│                     ↓                                          │
│                  Layer 4: MTF Bias Engine                      │
│                     ↓                                          │
│                  Layer 5: Cooldown & Dedup Gate                │
│                     ↓                                          │
│                  Layer 6: LLM Reasoning Engine                 │
│                     ↓                                          │
│                  Layer 7: Filter Gate (10 filters)             │
│                     ↓                                          │
│                  Layer 8: Risk & Sizing Engine                 │
│                     ↓                                          │
│                  Layer 9: Execution & Delivery (Telegram)      │
│                     ↓                                          │
│              ┌──── Layer 10: Logging & Tracking ────┐          │
│              │          SQLite Database              │          │
│              └───────────────────────────────────────┘          │
│              ┌──── Layer 11: Feedback & Optimization ─┐         │
│              │      Weekly Report / Prompt Tuning      │         │
│              └───────────────────────────────────────┘          │
│              ┌──── Layer 12: Health Monitoring ────────┐         │
│              │    Watchdog / 8080 Health Endpoint       │         │
│              └───────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Key Technical Decisions

| Decision | Rationale |
|----------|-----------|
| Single asyncio process | Simpler deployment, no container orchestration needed, lower AWS cost |
| SQLite instead of PostgreSQL | Single-user system; no concurrent write contention; zero maintenance |
| LLM via 9router (hermes-main) | Routes to Claude Opus 4.6 — cheaper than direct OpenAI API, better reasoning |
| Telegram bot (not web UI) | Instant delivery, mobile-friendly, zero frontend development |
| Paper trading built-in | Test strategies with $10K virtual balance before going live |
| systemd supervision | Zero-dependency auto-restart on crash; built-in logging rotation |

### 1.3 Runtime Environment

| Parameter | Value |
|-----------|-------|
| Process type | Single async Python 3.11+ process |
| Event loop | asyncio |
| Supervision | systemd (restart always, 5s delay) |
| Memory baseline | ~480 MB peak |
| CPU usage | <5% idle, spikes to 20% during LLM calls |
| Uptime target | 99.9% (only down for deploys) |

---

## 2. Module Specifications

### 2.1 Layer 0 — Data Ingestion

**Files:** `data/websocket_feed.py`, `data/fetcher.py`, `data/multi_asset_feed.py`

**Purpose:** Establish and maintain live data streams from Binance and fetch historical OHLCV data.

**WebSocket Feed (`WebSocketFeed`):**
- Connects to Binance WebSocket Stream API for 1-minute kline data
- Supports multiple symbol streams concurrently: `btcusdt@kline_1m`, `ethusdt@kline_1m`, etc.
- Auto-reconnect on disconnect with exponential backoff (1s → 5s → 30s cap)
- Each tick updates a shared `latest_prices` dict with symbol → price mapping
- Fires an `on_tick` callback on each new candle close

**OHLCV Fetcher (`DataFetcher`):**
- Uses ccxt to fetch historical OHLCV data from Binance REST API
- Supports configurable timeframes: `1h`, `4h`, `1d`
- Configurable lookback: 300 bars per timeframe by default
- Implements rate limiting (max 10 requests per second)
- Fallback to cached data on API failure

**Multi-Asset Feed (`MultiAssetFeed`):**
- Orchestrates WebSocket connections for all enabled assets
- Manages connection lifecycle (start, stop, health check)
- Tracks per-symbol tick counts and last tick timestamps

**Configuration:**
```python
# config/assets.py
ASSETS = [
    AssetConfig(symbol="BTC/USDT", binance_symbol="BTCUSDT", timeframes=["1h","4h","1d"], primary_tf="1h", enabled=True),
    AssetConfig(symbol="ETH/USDT", binance_symbol="ETHUSDT", timeframes=["1h","4h","1d"], primary_tf="1h", enabled=True),
    AssetConfig(symbol="BNB/USDT", binance_symbol="BNBUSDT", timeframes=["1h","4h","1d"], primary_tf="1h", enabled=True),
    AssetConfig(symbol="SOL/USDT", binance_symbol="SOLUSDT", timeframes=["1h","4h","1d"], primary_tf="1h", enabled=True),
]
```

**Edge Cases Handled:**
- WebSocket disconnect during idle periods → auto-reconnect within 30s
- Late candle data (exchange delay) → accept up to 5s late
- Empty response from ccxt REST → retry with exponential backoff, fall back to cache

---

### 2.2 Layer 1 — Feature Engineering

**Files:** `signals/pipeline.py`

**Purpose:** Transform raw OHLCV data into calculated technical indicators and structural features.

**Indicators Calculated (via pandas-ta):**
- **Momentum:** RSI(14), Stochastic(14,3,3), CCI(20), Williams %R(14), MFI(14)
- **Trend:** EMA(20/50/200), VWAP, Supertrend(10,3), Ichimoku Cloud, ADX(14)
- **Volatility:** ATR(14), Bollinger Bands(20,2), Keltner Channel
- **Volume:** OBV, RVOL (relative volume), Volume Profile POC
- **MACD:** 12, 26, 9 — line, signal, histogram, histogram direction

**Structural Features:**
- Swing points (pivot highs/lows with configurable lookback)
- Market structure state (uptrend/downtrend/ranging based on HH/HL/LH/LL)
- S/R levels (swing point clustering within 0.3% tolerance)
- Premium/Discount zone (50% Fibonacci range midpoint)

**Candlestick Pattern Detection:**
- Single candle: Hammer, Shooting Star, Doji (Dragonfly, Gravestone, Standard)
- Two candle: Bullish/Bearish Engulfing, Piercing Line, Dark Cloud Cover
- Three candle: Morning Star, Evening Star, Three White Soldiers, Three Black Crows

**Update Frequency:** On each new 1m candle close from WebSocket, but full re-analysis only every 60 seconds (the SMC analysis cycle).

---

### 2.3 Layer 2 — Pattern & Structure Detection

**Files:** `signals/swing_detector.py`, `signals/fvg_detector.py`, `signals/srm_runner.py`

**Purpose:** Identify Smart Money Concepts (SMC/ICT) patterns and classic chart patterns.

**Swing Detector (`SwingDetector`):**
- Identifies swing highs and lows using pivot point analysis with configurable lookback
- Minimum swing height threshold to filter noise (0.3% of price)
- Tracks swing history for trend structure (HH/HL/LH/LL)

**Break of Structure (BOS):**
- Bullish BOS: price breaks above previous swing high in an uptrend
- Bearish BOS: price breaks below previous swing low in a downtrend
- Requires 1 candle close beyond the swing point for confirmation

**Change of Structure (ChOS):**
- Bullish ChOS: in a downtrend, price breaks above the most recent lower high
- Bearish ChOS: in an uptrend, price breaks below the most recent higher low
- Priority over BOS — signals potential trend reversal

**Fair Value Gap (FVG) Detection:**
- 3-candle imbalance: Candle 3 high/low does not overlap Candle 1 high/low
- Minimum gap size: 0.1% of price to filter noise
- Tracks FVG age (when formed, how many candles since)
- Identifies when price re-enters the FVG (mitigation)
- Supports both bullish FVG (gap up) and bearish FVG (gap down)

**Order Block (OB) Detection:**
- Last opposing candle before an impulsive move (body > 1.5× ATR)
- Bullish OB: last bearish candle before bullish impulse
- Bearish OB: last bullish candle before bearish impulse
- OB zone = candle open to close

**Liquidity Grab Detection:**
- Price wicks beyond a recent swing high/low
- Candle closes back inside the swing range
- Elevated volume on the sweep candle
- Reversal confirmation on the following candle

**Chart Pattern Detection:**
- Head & Shoulders / Inverse H&S
- Double Top / Double Bottom
- Ascending / Descending / Symmetrical Triangles
- Bull / Bear Flags
- Wedges (Rising / Falling)
- Cup & Handle
- TTM Squeeze (Bollinger Bands inside Keltner Channel)

**Divergence Detection:**
- RSI regular divergence (price HH + RSI LH = bearish divergence)
- MACD regular divergence (price HH + MACD LH)
- Hidden divergence (pullbacks in trends)
- Force index divergence

---

### 2.4 Layer 3 — Confluence Scoring Engine

**Files:** `signals/confluence.py`, `signals/mtf_bias.py`

**Purpose:** Aggregate all detected signals into a weighted confluence score.

**Signal Classification:**

| Tier | Weight | Signals |
|------|--------|---------|
| Tier 1 — Structure | 3× per signal | BOS/ChOS, Market Structure, S/R Break/Hold, OB Reaction, MTF Bias Alignment |
| Tier 2 — Trigger | 2× per signal | FVG Entry, Liquidity Grab + Reversal, Impulsive Candle at Key Level, Candlestick Reversal at S/R, BB/KC Squeeze Breakout, Volume Surge, Chart Pattern Breakout |
| Tier 3 — Confirmation | 1× per signal | RSI Alignment (<65/ >35), MACD Histogram direction, EMA Alignment, VWAP position, ADX >25, On-Chain Flow, Sentiment, Funding Rate, Killzone Timing, Correlation |

**Scoring Formula:**
```
Total Score = (Tier 1 signals × 3) + (Tier 2 signals × 2) + (Tier 3 signals × 1)
```

**Thresholds:**
| Score Range | Classification | Action |
|-------------|----------------|--------|
| 0–7 | SUPPRESSED | Log silently. No LLM call. No signal delivered. |
| 8–10 | STANDARD | Trigger LLM. Normal position size if confidence ≥75%. |
| 11–14 | STRONG | Trigger LLM. Priority delivery. |
| 15+ | PREMIUM | Trigger LLM. Max allowable position size. |

**MTF Bias Engine:**
- Calculates directional bias for 1D, 4H, and 1H timeframes
- If all three do not agree on direction → signals suppressed until alignment
- Bias determined by: EMA alignment (20/50/200), HH/HL structure, ADX direction
- Runs on each new 4H candle close and on-demand during SMC analysis cycle

---

### 2.5 Layer 4 — MTF Bias Engine

*See Layer 3 — MTF Bias Engine section above for full specification.*

---

### 2.6 Layer 5 — Cooldown & Dedup Gate

**Files:** `db/schema.py` (cooldowns table)

**Purpose:** Prevent signal spam, revenge trading, and duplicate signals.

**Cooldown Rules:**

| Condition | Cooldown Duration |
|-----------|-------------------|
| Normal signal delivered | 30 minutes |
| Stop loss hit | 2 hours (revenge trade prevention) |
| Consecutive losses (3+) | 4 hours |
| Daily loss limit hit (4%) | Until next trading day |

**Implementation:**
- Cooldowns stored in SQLite `cooldowns` table with asset symbol and expiry timestamp
- Checked before Layer 6 (LLM) to avoid unnecessary LLM costs
- Dedup: same direction signal for same asset within cooldown → suppressed

---

### 2.7 Layer 6 — LLM Reasoning Engine

**Files:** `signals/llm_engine.py`, `signals/prompt_builder.py`

**Purpose:** Use a Large Language Model to evaluate the full market context and decide whether to issue a trading signal.

**Model Configuration:**
```ini
OPENAI_API_KEY=sk-***           # 9router API key
OPENAI_BASE_URL=http://localhost:20128/v1
OPENAI_MODEL=hermes-main         # Routes to Claude Opus 4.6 via 9router
OPENAI_FALLBACK=hermes-main      # Same model for fallback (9router handles failover internally)
```

**Prompt Structure:**
The LLM prompt (built by `prompt_builder.py`) includes:
1. System instruction defining the LLM's role as an expert SMC/ICT trader
2. Current market context: price, timeframe bias (1D/4H/1H), support/resistance levels
3. All detected SMC/ICT features: BOS, ChOS, FVGs, OBs, liquidity grabs
4. Technical indicators: RSI, MACD, EMA alignment, ATR, Bollinger Bands
5. Candlestick patterns detected
6. External data: Fear & Greed Index, news sentiment (if available), correlations
7. Current open positions and account context
8. Output format specification (JSON with strict schema)

**LLM Response Schema:**
```json
{
  "signal": "LONG" | "SHORT" | "PASS",
  "confidence": 0-100,
  "entry_price": float,
  "tp1/tp2/tp3": float,
  "stop_loss": float,
  "reasoning": "string",
  "invalidation": "string"
}
```

**Retry Logic:**
- Max retries: 3 (LLM_MAX_RETRIES)
- Backoff: 1s linear between retries
- Fallback: Uses OPENAI_FALLBACK model on last attempt
- Timeout: 60s per request (LLM_TIMEOUT)
- On total failure: returns PASS signal with "API error" reason

---

### 2.8 Layer 7 — Filter Gate

**Files:** `signals/pipeline.py` (filter gate logic in SMC analysis loop)

**Purpose:** Apply 10 independent filters that ALL must pass for signal delivery.

| # | Filter | Condition | Implementation |
|---|--------|-----------|----------------|
| 1 | Confidence | LLM confidence ≥ 75% | Compare `signal.confidence >= 75` |
| 2 | Risk/Reward | R:R ratio ≥ 1.5 | Compare entry to SL/TP distances |
| 3 | MTF Alignment | Daily + 4H + 1H agree | Bias engine output check |
| 4 | Cooldown | No signal for this asset in 30–60 min | SQLite cooldowns table lookup |
| 5 | Active Signals Cap | Open positions < 3 | Position tracker count |
| 6 | Portfolio Heat | Total open risk < 6% of account | Sum of all open position risk |
| 7 | News Buffer | No high-impact event within ±2h | Economic calendar check |
| 8 | Volatility Regime | ATR not in extreme (>3σ above avg) | ATR stats comparison |
| 9 | Sentiment Extreme | Fear & Greed between 10 and 90 | F&G index check |
| 10 | Spread | Bid-ask spread < 0.1% of price | Orderbook check (planned) |

---

### 2.9 Layer 8 — Risk & Sizing Engine

**Files:** `trading/paper_trade.py`

**Purpose:** Calculate dynamic position sizes based on LLM confidence and account risk parameters.

**Formula:**
```
Position Size = (Account Balance × Risk%) ÷ |Entry Price − Stop Loss Price|
```

**Risk Tiers by Confidence:**

| Confidence | Account Risk % |
|------------|----------------|
| 75–80% | 1.0% |
| 80–90% | 1.5% |
| 90%+ | 2.0% |

**Hard Rules (System-Enforced):**
- Max risk per trade: 2% of account
- Max concurrent positions: 3
- Max portfolio heat: 6%
- Stop loss mandatory on every signal
- Daily loss limit: 4% (auto-pause)
- Weekly loss limit: 8% (reduce max positions to 2)

**Take Profit Strategy:**
| Level | Close % | Action |
|-------|---------|--------|
| TP1 | 40% | Move SL to breakeven |
| TP2 | 30% | Trail SL below last HL (longs) |
| TP3 | 30% (remainder) | Position closed |

---

### 2.10 Layer 9 — Execution & Delivery

**Files:** `delivery/telegram_bot.py`, `delivery/telegram_commands.py`

**Purpose:** Format and deliver signals to the user via Telegram bot.

**Telegram Bot (`TelegramBot`):**
- Uses direct HTTP API calls (no python-telegram-bot library required)
- Sends messages with `parse_mode=HTML` for formatted output
- Emoji indicators: 🟢 LONG / 🔴 SHORT
- Supports `/` commands via long-polling (getUpdates)

**Telegram Commands (`TelegramCommandHandler`):**
| Command | Description | Response |
|---------|-------------|----------|
| `/status` or `/positions` | Show open positions | List of active papers with entry, SL, TP |
| `/stats` | Show 7-day performance | Win rate, avg R:R, profit factor, P&L |
| `/history [days]` | Show recent closed positions | Last N closed trades with P&L |
| `/health` | Show system component health | pipeline, llm, database, websocket status |
| `/close <id> <price>` | Manually close a position | Confirmation with P&L |

**Signal Message Format:**
```
🟢 LONG BTC/USDT
Entry: $67,420
TP1: $68,900 (+2.2%) — 40%
TP2: $69,800 (+3.5%) — 30%
TP3: $71,200 (+5.6%) — 30%
SL: $66,100 (-2.0%)
R:R 1:2.8 | Confidence: 84% | Risk: 1.5%

Reasoning: [LLM reasoning text]
```

**Bot Configuration:**
- Token: from `TELEGRAM_BOT_TOKEN` env var
- Chat ID: from `TELEGRAM_CHAT_ID` env var
- Username: `@tradingforgee_bot`

---

### 2.11 Layer 10 — Logging & Tracking

**Files:** `db/schema.py`, `signals/position_tracker.py`, `signals/win_rate.py`

**Purpose:** Log every signal and trade outcome with full context for audit and performance analysis.

**Database:** SQLite at `db/signalforge.db`

All signals (delivered and suppressed) are logged with:
- Timestamp, symbol, direction, entry price
- Confluence score breakdown (Tier 1/2/3 count)
- LLM confidence, model used, latency
- Filter gate results (which filters passed/failed)
- Execution status (delivered/suppressed/pending)

Trade tracking:
- Auto-detection of TP/SL hits via paper engine
- Every closed trade logged with P&L (USD and %)
- Win/loss classification
- Average R:R realized vs expected

---

### 2.12 Layer 11 — Feedback & Optimization

**Files:** `monitoring/weekly_report.py`, `scripts/send_weekly_report.py`

**Purpose:** Generate weekly performance reports and feed results back into system improvement.

**Weekly Report (every Monday 07:00 WIB):**
- Win rate (7-day rolling)
- Number of trades and signals
- Average R:R (expected vs realized)
- Profit factor
- Best/worst setup analysis
- System uptime

**Cron Schedule:**
- Runs every Sunday 00:00 UTC via Hermes cron
- No-agent mode (0 token cost)
- Script at `scripts/send_weekly_report.py`

---

### 2.13 Layer 12 — Health Monitoring

**Files:** `monitoring/health_endpoint.py`, `monitoring/watchdog.py`, `monitoring/error_alerter.py`

**Purpose:** Ensure system reliability with automatic failure detection and alerting.

**Health HTTP Endpoint (port 8080):**
```
GET /health → {"status": "ok", "uptime_seconds": N, "components": {...}}
GET /health/ws → {"symbols": {"BTC/USDT": {"connected": true, "last_tick_age_s": N, "total_ticks": N}}, "total_symbols": N}
```

**Component Health Tracking:**
| Component | Status Values | Details |
|-----------|---------------|---------|
| pipeline | ok / degraded / down | Cycle completion, assets analyzed |
| llm | ok / degraded / down | Model used, latest response time |
| database | ok / degraded / down | Connection status |
| websocket | ok / degraded / down | Symbols connected, live status |

**Health Watchdog:**
- Runs every 5 minutes
- Checks: WebSocket connection, LLM API responsiveness, data feed freshness, disk space, RAM usage
- Triggers Telegram alert on any failure
- Systemd auto-restart on crash (RestartSec=5)

**Error Alerter:**
- Monitors log file for ERROR level messages
- Sends Telegram alert on critical errors (pipeline crash, API failure, WS disconnect)
- Runs every 60 seconds (configurable)
- Flood control: same error deduped within 5 minutes

---

## 3. Data Flow Architecture

### 3.1 Main Loop (60-second cycle)

```
1. Fetch OHLCV for all 4 assets (1H, 4H, 1D)
2. For each asset (3s delay between assets):
   a. Calculate indicators (RSI, MACD, EMA, ATR, BB, etc.)
   b. Detect SMC structures (BOS, ChOS, FVG, OB, liquidity grab)
   c. Score confluence (Tier 1 + Tier 2 + Tier 3)
   d. If score >= 8:
      - Check cooldown
      - Build LLM prompt with full context
      - Call LLM (hermes-main → Claude Opus 4.6)
      - Parse LLM response
      - Apply filter gate (10 filters)
      - If all pass: calculate position size, deliver signal via Telegram
3. Paper engine auto-tick: check open positions for TP/SL hits
4. Update health status
5. Sleep until next 60s cycle
```

### 3.2 WebSocket Data Flow (Continuous)

```
Binance WS Stream → on_tick callback → update latest_prices dict
                                     → update per-symbol tick count
                                     → update WS health status
```

### 3.3 Command Flow (On-Demand)

```
User sends /status → getUpdates polling → TelegramCommandHandler
                                       → query PositionTracker
                                       → format HTML response
                                       → send via TelegramBot.send()
```

---

## 4. API Specifications

### 4.1 Internal Health API

**Endpoint: `GET /health`**

Response:
```json
{
  "status": "ok",
  "uptime_seconds": 3600.5,
  "components": {
    "pipeline": {"status": "ok", "details": "Cycle complete, 4 assets analyzed"},
    "llm": {"status": "ok", "details": "Model: hermes-main"},
    "database": {"status": "ok", "details": ""},
    "websocket": {"status": "ok", "details": ""}
  },
  "version": "1.0"
}
```

**Endpoint: `GET /health/ws`**

Response:
```json
{
  "symbols": {
    "BTC/USDT": {"connected": true, "last_tick_age_s": 3.5, "total_ticks": 1520},
    "ETH/USDT": {"connected": true, "last_tick_age_s": 3.5, "total_ticks": 1520}
  },
  "total_symbols": 4
}
```

### 4.2 Telegram Bot Commands

| Command | HTTP Method | Parameters | Response |
|---------|-------------|------------|----------|
| `/status` | getUpdates → handle | None | HTML list of open positions |
| `/stats` | getUpdates → handle | None | HTML performance summary |
| `/history [days]` | getUpdates → handle | days (int, default 7) | HTML closed positions list |
| `/health` | getUpdates → handle | None | HTML component status |
| `/close <id> <price>` | getUpdates → handle | id (int), price (float) | HTML confirmation |

Telegram API called internally:
```
POST https://api.telegram.org/bot<TOKEN>/sendMessage
  chat_id: <CHAT_ID>
  text: <HTML formatted message>
  parse_mode: HTML
```

---

## 5. Database Schema

**File:** `db/signalforge.db`

### 5.1 Entity Relationship Diagram (Textual)

```
candles (1) ──→ signals (∞) ──→ paper_trades (∞) ──→ paper_balance (1)
                                     │
                                     └──→ positions (∞)
                                     
signals (∞) ──→ cooldowns (1) [per symbol]
```

### 5.2 Table Definitions

**`candles` table:**
```sql
CREATE TABLE candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    UNIQUE(symbol, timeframe, timestamp)
);
```

**`signals` table:**
```sql
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT,
    entry_price REAL,
    confidence INTEGER,
    rr_ratio REAL,
    confluence_score INTEGER,
    model_used TEXT,
    filter_results TEXT,
    status TEXT DEFAULT 'pending',
    delivered INTEGER DEFAULT 0,
    reasoning TEXT,
    error TEXT
);
```

**`positions` table:**
```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT,
    entry_price REAL,
    stop_loss REAL,
    tp1 REAL, tp2 REAL, tp3 REAL,
    position_size REAL,
    status TEXT DEFAULT 'OPEN',
    opened_at INTEGER,
    closed_at INTEGER,
    pnl_usd REAL,
    pnl_pct REAL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
```

**`paper_trades` table:**
```sql
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT,
    entry_price REAL,
    sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
    position_size REAL,
    signal_id INTEGER,
    confidence INTEGER,
    status TEXT DEFAULT 'OPEN',
    open_price REAL,
    close_price REAL,
    pnl_usd REAL, pnl_pct REAL,
    rr_realized REAL,
    tp1_hit INTEGER DEFAULT 0,
    tp2_hit INTEGER DEFAULT 0,
    sl_hit INTEGER DEFAULT 0,
    opened_at INTEGER,
    closed_at INTEGER
);
```

**`paper_balance` table:**
```sql
CREATE TABLE paper_balance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance REAL NOT NULL DEFAULT 10000.0,
    updated_at INTEGER NOT NULL
);
```

**`cooldowns` table:**
```sql
CREATE TABLE cooldowns (
    symbol TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
```

**`health_log` table:**
```sql
CREATE TABLE health_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    component TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT
);
```

**`build_log` table:**
```sql
CREATE TABLE build_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint INTEGER,
    timestamp INTEGER NOT NULL,
    event TEXT NOT NULL,
    details TEXT
);
```

---

## 6. Error Handling & Edge Cases

### 6.1 Error Recovery Strategy

| Error Type | Detection | Recovery |
|------------|-----------|----------|
| WebSocket disconnect | No tick for 60s | Auto-reconnect (exponential backoff 1s→30s) |
| LLM API timeout | 60s timeout on request | Retry up to 3 times, then PASS |
| LLM JSON parse failure | Invalid JSON response | Retry (LLM regenerates), then PASS |
| Binance REST rate limit | HTTP 429 from ccxt | Exponential backoff, use cached data |
| SQLite write failure | sqlite3.OperationalError | Retry after 1s; if persistent → alert |
| system crash | Process exits | systemd auto-restart (RestartSec=5) |
| Memory leak | Memory > 1GB threshold | Health watchdog triggers restart |
| Disk full | Write failure | Alert via Telegram, graceful shutdown |

### 6.2 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| No trades yet (new deployment) | `/status` returns "No open positions" |
| All LLM calls fail | Returns PASS, logged with error detail |
| Cooldown expires during LLM call | Checked at delivery, not at trigger |
| Multiple signals same minute | Dedup by symbol + direction |
| Empty WebSocket feed on startup | Graceful degradation: uses REST data until WS connects |
| SOL/USDT volatile spikes | Higher min_confluence_score (9 vs 8) |
| 4 assets exceed processing window | 3s stagger between assets keeps cycle under 60s |

---

## 7. Performance Requirements

| Metric | Requirement | Actual (Measured) |
|--------|-------------|-------------------|
| SMC analysis cycle time | <60s for 4 assets | ~20-25s (with LLM calls) |
| WebSocket tick latency | <500ms from exchange | ~100-300ms |
| LLM response time | <30s per call | ~15-20s (Claude Opus) |
| REST OHLCV fetch | <5s per asset | ~1-2s |
| Telegram delivery | <2s from decision | ~500ms |
| Memory footprint | <1GB RSS | ~480-530 MB peak |
| CPU usage (idle) | <10% | ~3-5% |
| CPU usage (active) | <50% | ~15-25% |
| Database size (30 days) | <100MB | ~2MB currently |
| Concurrent WS streams | 10+ | 4 currently, scalable to 20+ |

---

## 8. Configuration Reference

### 8.1 Environment Variables (.env)

```ini
# === Telegram ===
TELEGRAM_BOT_TOKEN=***         # Bot token from @BotFather
TELEGRAM_CHAT_ID=955169177     # Your Telegram chat ID

# === LLM (9router) ===
OPENAI_API_KEY=sk-***          # 9router API key
OPENAI_BASE_URL=http://localhost:20128/v1
OPENAI_MODEL=hermes-main       # Routes to Claude Opus 4.6
OPENAI_FALLBACK=hermes-main    # Same model for fallback

# === Trading ===
PAPER_TRADING=true             # true = paper mode, false = live signals
```

### 8.2 Asset Configuration (config/assets.py)

See Section 2.1 for `AssetConfig` dataclass and `ASSETS` list. Key parameters per asset:
- `symbol`: Trading pair (e.g., "BTC/USDT")
- `binance_symbol`: Binance format (e.g., "BTCUSDT")
- `timeframes`: List of timeframes ["1h", "4h", "1d"]
- `primary_tf`: Entry timeframe (usually "1h")
- `min_confluence_score`: Threshold per asset (default 8, SOL=9)
- `min_rr`: Minimum risk/reward (default 1.5)
- `enabled`: True/False to include in pipeline

---

## 9. Approval

| Role | Name | Date | Status |
|------|------|------|--------|
| Product Owner | Fazrial | ___/___/2026 | Pending |
| Developer | Hermes Agent | 25/06/2026 | Ready |
| Reviewer | | ___/___/2026 | Pending |

---

*End of Document — SignalForge FSD v1.0*
*Confidential — Do not distribute without authorization.*
