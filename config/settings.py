"""SignalForge Configuration"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / "config" / ".env")

# --- TELEGRAM
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Primary chat (DM) — used for command auth default + backward-compatible single target
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
# Optional extra destinations for dual/multi delivery (comma-separated).
# Example: TELEGRAM_EXTRA_CHAT_IDS=-1003811405386
# All outbound signals/alerts fan out to TELEGRAM_CHAT_ID + these extras.
# Command replies stay in the chat that sent the command.
_EXTRA_RAW = os.environ.get("TELEGRAM_EXTRA_CHAT_IDS", "")
TELEGRAM_EXTRA_CHAT_IDS = [c.strip() for c in _EXTRA_RAW.split(",") if c.strip()]
# Deduped ordered list: primary first, then extras
_seen_chat_ids: set[str] = set()
TELEGRAM_CHAT_IDS: list[str] = []
for _cid in [TELEGRAM_CHAT_ID, *TELEGRAM_EXTRA_CHAT_IDS]:
    if _cid and _cid not in _seen_chat_ids:
        TELEGRAM_CHAT_IDS.append(_cid)
        _seen_chat_ids.add(_cid)
del _seen_chat_ids

# --- LLM
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL    = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL       = os.environ.get("OPENAI_MODEL", "openai/gpt-4o")
OPENAI_FALLBACK    = os.environ.get("OPENAI_FALLBACK", "openai/gpt-4o-mini")
LLM_TIMEOUT        = int(os.environ.get("LLM_TIMEOUT", "45"))
LLM_MAX_RETRIES    = int(os.environ.get("LLM_MAX_RETRIES", "2"))
LLM_PROMPT_VERSION = "v1.0"

# --- EXCHANGE
EXCHANGE     = "binance"
SYMBOL       = "BTC/USDT"
TIMEFRAMES   = ["1m", "5m", "15m", "1h", "4h", "1d"]
_PRIMARY_TF_RAW = os.environ.get("PRIMARY_TF", "5m")
if _PRIMARY_TF_RAW not in TIMEFRAMES:
    raise ValueError(
        f"PRIMARY_TF='{_PRIMARY_TF_RAW}' is not in TIMEFRAMES={TIMEFRAMES}. "
        "Fix config/.env or PRIMARY_TF env var."
    )
PRIMARY_TF   = _PRIMARY_TF_RAW
BIAS_TF      = os.environ.get("BIAS_TF",  "15m")
MACRO_TF     = os.environ.get("MACRO_TF", "1h")
HISTORY_BARS = 500

# --- CONFLUENCE
# All three thresholds are env-overridable so A/B testing and tuning
# don't require a code deploy — just change .env and restart.
MIN_CONFLUENCE_SCORE = int(float(os.environ.get("MIN_CONFLUENCE_SCORE", "6")))
MIN_LLM_CONFIDENCE   = float(os.environ.get("MIN_LLM_CONFIDENCE",   "65"))
MIN_RR_RATIO         = float(os.environ.get("MIN_RR_RATIO",          "1.0"))  # 1.0 minimum for scalping; override per-asset via AssetConfig.min_rr
TIER1_WEIGHT = 3
TIER2_WEIGHT = 2
TIER3_WEIGHT = 1

# --- RISK
ACCOUNT_BALANCE        = float(os.environ.get("ACCOUNT_BALANCE", "10000"))
# Risk % overridable via env so paper vs live accounts can differ without code changes
BASE_RISK_PCT          = float(os.environ.get("BASE_RISK_PCT",  "1.0"))
MID_RISK_PCT           = float(os.environ.get("MID_RISK_PCT",   "1.5"))
HIGH_RISK_PCT          = float(os.environ.get("HIGH_RISK_PCT",  "2.0"))
MAX_CONCURRENT         = 3
MAX_PORTFOLIO_HEAT_PCT = float(os.environ.get("MAX_PORTFOLIO_HEAT_PCT", "6.0"))
DAILY_LOSS_LIMIT_PCT   = float(os.environ.get("DAILY_LOSS_LIMIT_PCT",   "4.0"))
# WEEKLY_LOSS_LIMIT_PCT: enforced in filter_gate._f6c_weekly_loss_limit
# (Sprint 13). Loads from env the same way as DAILY_LOSS_LIMIT_PCT.
WEEKLY_LOSS_LIMIT_PCT  = float(os.environ.get("WEEKLY_LOSS_LIMIT_PCT",  "8.0"))
MIN_ATR_DISTANCE       = 0.5

# --- FILTER GATE
COOLDOWN_MINUTES    = 10
SL_COOLDOWN_MINUTES = 30
MAX_SPREAD_PCT      = 0.1
NEWS_BUFFER_HOURS   = 2
FEAR_GREED_MIN      = 10
FEAR_GREED_MAX      = 90

# --- INDICATORS
RSI_PERIOD   = 14
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
EMA_SHORT    = 20
EMA_MID      = 50
EMA_LONG     = 200
ATR_PERIOD   = 14
BB_PERIOD    = 20
BB_STD       = 2.0
ADX_PERIOD   = 14
STOCH_K      = 14
STOCH_D      = 3
STOCH_SMOOTH = 3
VOLUME_MA    = 20

# --- SMC
SWING_LOOKBACK    = 3
FVG_MIN_SIZE_PCT  = 0.1
IMPULSE_ATR_MULT  = 1.5
IMPULSE_VOL_MULT  = 2.0

# --- MTF GATE
# Minimum alignment strength for filter_3. Swing mode requires 2/3 TFs to agree
# (0.67); scalping mode accepts 1/3 (0.33) since 5m can lead higher TFs briefly.
# 1.0 = all 3 TFs aligned (old binary behaviour — too strict for swing setups).
MTF_STRENGTH_MIN_SWING  = 0.67
MTF_STRENGTH_MIN_SCALP  = 0.33

# --- BOS RETEST ENTRY (S-3)
# ICT pullback model — arm on BOS, wait for price to tap FVG/OB left by
# the impulse candle before firing the LLM. Improves entry timing vs
# firing immediately on BOS close.
#
# BOS_RETEST_ENABLED              — set False to revert to legacy immediate-fire
# BOS_RETEST_TTL_BARS             — cycles to wait before expiring ARMED state
#                                   (1 cycle = ~60s in main loop)
# BOS_RETEST_FVG_TOLERANCE        — max % distance from impulse close to accept FVG
# BOS_RETEST_OB_TOLERANCE         — max % distance from impulse close to accept OB
# BOS_RETEST_REQUIRE_REVERSAL_CANDLE — require a reversal candle at the zone before firing
BOS_RETEST_ENABLED                 = True
BOS_RETEST_TTL_BARS                = 20      # ~20 minutes at 1 cycle/min (was 12 — too tight for 5m pullbacks)
BOS_RETEST_FVG_TOLERANCE           = 0.015   # 1.5% from impulse close (was 1% — too tight for volatile alts)
BOS_RETEST_OB_TOLERANCE            = 0.02    # 2% from impulse close (scalping: tighter zones)
BOS_RETEST_REQUIRE_REVERSAL_CANDLE = True    # require hammer/engulfing etc at zone

# --- LLM CONCURRENCY
# Max simultaneous LLM calls across all symbols. The 9router provider enforces
# a 3-request concurrent limit — exceeding it causes 429 errors. Set to 2 to
# leave headroom for retries without hitting the cap.
LLM_MAX_CONCURRENT = 2

# --- ATR VOLATILITY FILTER (filter_8)
# Period for ATR calculation (standard = 14 bars).
ATR_PERIOD            = 14
# Rolling window to compute the baseline average ATR.
ATR_AVG_PERIOD        = 20
# Block signals when current ATR > ATR_SPIKE_MULTIPLIER × average ATR.
# 2.5× = "price is moving 2.5× its normal range" — common spike threshold.
ATR_SPIKE_MULTIPLIER  = 2.5
# Minimum bars required before the filter runs.  Below this the filter
# skips (fail-safe pass) to avoid false blocks on short history.
ATR_MIN_BARS          = 40   # ATR_PERIOD + ATR_AVG_PERIOD + headroom

# --- SESSION FILTER
# Alt coins only — BTC/ETH trade 24/7. Alts blocked outside London/NY open.
# London: 07:00–16:00 UTC. NY: 13:00–22:00 UTC. Overlap: 13:00–16:00 UTC.
# Crypto trades 24/7 — no session restriction needed.
# All assets including alts are active around the clock.
# TIER1 assets bypass session filter regardless.
SESSION_FILTER_ENABLED   = False  # disabled — crypto is 24/7
SESSION_ACTIVE_START_UTC = 0   # 00:00 UTC (full 24h window)
SESSION_ACTIVE_END_UTC   = 23  # 23:00 UTC (full 24h window)
TIER1_ASSETS = ["BTC/USDT", "ETH/USDT"]  # always allowed, no session restriction

# --- HEALTH
WATCHDOG_INTERVAL   = 300
WS_STALE_SECONDS    = 120
DAILY_PING_HOUR_UTC = 0

# --- PATHS
DB_PATH     = str(BASE_DIR / "db" / "signalforge.db")
LOG_PATH    = str(BASE_DIR / "logs" / "signalforge.log")
STATUS_PATH = str(BASE_DIR / "config" / "build_status.json")
