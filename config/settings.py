"""SignalForge Configuration"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / "config" / ".env")

# --- TELEGRAM
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

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
PRIMARY_TF   = "5m"
BIAS_TF      = "15m"
MACRO_TF     = "1h"
HISTORY_BARS = 500

# --- CONFLUENCE
MIN_CONFLUENCE_SCORE = 6
MIN_LLM_CONFIDENCE   = 65
MIN_RR_RATIO         = 1.8  # Raised from 1.2 — 1.2 barely covers 0.2% round-trip fees on tight scalp stops
TIER1_WEIGHT = 3
TIER2_WEIGHT = 2
TIER3_WEIGHT = 1

# --- RISK
ACCOUNT_BALANCE        = float(os.environ.get("ACCOUNT_BALANCE", "10000"))
BASE_RISK_PCT          = 1.0
MID_RISK_PCT           = 1.5
HIGH_RISK_PCT          = 2.0
MAX_CONCURRENT         = 3
MAX_PORTFOLIO_HEAT_PCT = 6.0
DAILY_LOSS_LIMIT_PCT   = 4.0
WEEKLY_LOSS_LIMIT_PCT  = 8.0
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

# --- HEALTH
WATCHDOG_INTERVAL   = 300
WS_STALE_SECONDS    = 120
DAILY_PING_HOUR_UTC = 0

# --- PATHS
DB_PATH     = str(BASE_DIR / "db" / "signalforge.db")
LOG_PATH    = str(BASE_DIR / "logs" / "signalforge.log")
STATUS_PATH = str(BASE_DIR / "config" / "build_status.json")
