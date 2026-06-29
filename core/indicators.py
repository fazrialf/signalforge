"""
SignalForge Core Indicator Calculator
Computes all technical indicators on a given OHLCV DataFrame.
Uses the 'ta' library (compatible with Python 3.14).
"""
import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def add_indicators(df: pd.DataFrame,
                   rsi_period: int = 14,
                   macd_fast: int = 12,
                   macd_slow: int = 26,
                   macd_signal: int = 9,
                   ema_short: int = 20,
                   ema_mid: int = 50,
                   ema_long: int = 200,
                   atr_period: int = 14,
                   bb_period: int = 20,
                   bb_std: float = 2.0,
                   adx_period: int = 14,
                   stoch_k: int = 14,
                   stoch_d: int = 3,
                   stoch_smooth: int = 3,
                   volume_ma: int = 20) -> pd.DataFrame:
    """
    Adds all indicators to the DataFrame in-place and returns it.
    Requires columns: open, high, low, close, volume
    """
    df = df.copy()
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # ---- RSI -------------------------------------------------------
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_loss = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ---- MACD ------------------------------------------------------
    ema_f = close.ewm(span=macd_fast, adjust=False).mean()
    ema_s = close.ewm(span=macd_slow, adjust=False).mean()
    df["macd"]        = ema_f - ema_s
    df["macd_signal"] = df["macd"].ewm(span=macd_signal, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # ---- EMAs ------------------------------------------------------
    df[f"ema_{ema_short}"] = close.ewm(span=ema_short, adjust=False).mean()
    df[f"ema_{ema_mid}"]   = close.ewm(span=ema_mid,   adjust=False).mean()
    df[f"ema_{ema_long}"]  = close.ewm(span=ema_long,  adjust=False).mean()

    # ---- ATR -------------------------------------------------------
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low  - close.shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=atr_period - 1, min_periods=atr_period).mean()

    # ---- Bollinger Bands -------------------------------------------
    bb_mid               = close.rolling(bb_period).mean()
    bb_std_val           = close.rolling(bb_period).std()
    df["bb_upper"]       = bb_mid + bb_std * bb_std_val
    df["bb_mid"]         = bb_mid
    df["bb_lower"]       = bb_mid - bb_std * bb_std_val
    df["bb_width"]       = (df["bb_upper"] - df["bb_lower"]) / bb_mid
    df["bb_pct"]         = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

    # ---- Keltner Channel (for squeeze) -----------------------------
    kc_mid               = close.ewm(span=20, adjust=False).mean()
    df["kc_upper"]       = kc_mid + 1.5 * df["atr"]
    df["kc_lower"]       = kc_mid - 1.5 * df["atr"]
    # Squeeze: BB inside KC
    df["kc_squeeze"]     = (
        (df["bb_upper"] < df["kc_upper"]) &
        (df["bb_lower"] > df["kc_lower"])
    ).astype(int)

    # ---- VWAP (session-level reset would require tick data;
    #            using rolling 20-bar as proxy) ----------------------
    tp = (high + low + close) / 3
    df["vwap"] = (tp * volume).rolling(20).sum() / volume.rolling(20).sum()

    # ---- Volume MA & RVOL ------------------------------------------
    df["vol_ma"]  = volume.rolling(volume_ma).mean()
    df["rvol"]    = volume / df["vol_ma"]   # relative volume ratio

    # ---- OBV -------------------------------------------------------
    direction     = np.sign(close.diff()).fillna(0)
    df["obv"]     = (volume * direction).cumsum()

    # ---- Stochastic ------------------------------------------------
    lowest_low    = low.rolling(stoch_k).min()
    highest_high  = high.rolling(stoch_k).max()
    stoch_raw     = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    df["stoch_k"] = stoch_raw.rolling(stoch_smooth).mean()
    df["stoch_d"] = df["stoch_k"].rolling(stoch_d).mean()

    # ---- ADX -------------------------------------------------------
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    smoothed_tr       = tr.ewm(com=adx_period - 1, min_periods=adx_period).mean()
    smoothed_plus_dm  = plus_dm.ewm(com=adx_period - 1, min_periods=adx_period).mean()
    smoothed_minus_dm = minus_dm.ewm(com=adx_period - 1, min_periods=adx_period).mean()
    plus_di   = 100 * smoothed_plus_dm  / smoothed_tr.replace(0, np.nan)
    minus_di  = 100 * smoothed_minus_dm / smoothed_tr.replace(0, np.nan)
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"]      = dx.ewm(com=adx_period - 1, min_periods=adx_period).mean()
    df["plus_di"]  = plus_di
    df["minus_di"] = minus_di

    # ---- Supertrend ------------------------------------------------
    # factor=3.0, period=10
    st_period = 10
    st_mult   = 3.0
    st_atr    = tr.ewm(com=st_period - 1, min_periods=st_period).mean()
    hl2       = (high + low) / 2
    upper_band = hl2 + st_mult * st_atr
    lower_band = hl2 - st_mult * st_atr

    supertrend = pd.Series(index=df.index, dtype=float)
    direction_st = pd.Series(index=df.index, dtype=int)
    for i in range(1, len(df)):
        prev_close = close.iloc[i - 1]
        curr_close = close.iloc[i]
        ub = upper_band.iloc[i]
        lb = lower_band.iloc[i]
        prev_ub = upper_band.iloc[i - 1]
        prev_lb = lower_band.iloc[i - 1]

        # Adjust bands
        if ub < prev_ub or prev_close > prev_ub:
            upper_band.iloc[i] = ub
        else:
            upper_band.iloc[i] = prev_ub

        if lb > prev_lb or prev_close < prev_lb:
            lower_band.iloc[i] = lb
        else:
            lower_band.iloc[i] = prev_lb

        prev_dir = direction_st.iloc[i - 1] if i > 1 else 1
        if prev_dir == -1 and curr_close > upper_band.iloc[i]:
            direction_st.iloc[i] = 1
        elif prev_dir == 1 and curr_close < lower_band.iloc[i]:
            direction_st.iloc[i] = -1
        else:
            direction_st.iloc[i] = prev_dir if not pd.isna(prev_dir) else 1

        supertrend.iloc[i] = (
            lower_band.iloc[i] if direction_st.iloc[i] == 1
            else upper_band.iloc[i]
        )

    df["supertrend"]    = supertrend
    df["supertrend_dir"] = direction_st  # 1=bullish, -1=bearish

    # ---- CCI -------------------------------------------------------
    df["cci"] = (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).std())

    # ---- Williams %R -----------------------------------------------
    hh14 = high.rolling(14).max()
    ll14  = low.rolling(14).min()
    df["williams_r"] = -100 * (hh14 - close) / (hh14 - ll14).replace(0, np.nan)

    # ---- Parabolic SAR (simplified) --------------------------------
    # Not computed here due to complexity; placeholder
    df["sar"] = np.nan  # filled by SMC module if needed

    return df


def get_snapshot(df: pd.DataFrame) -> dict:
    """
    Return a dict snapshot of the latest indicator values.
    Used to inject into LLM prompts.
    """
    if df.empty:
        return {}
    row = df.iloc[-1]

    def safe(val):
        try:
            return round(float(val), 4) if not pd.isna(val) else None
        except Exception:
            return None

    return {
        "close":       safe(row["close"]),
        "rsi":         safe(row["rsi"]),
        "macd":        safe(row["macd"]),
        "macd_signal": safe(row["macd_signal"]),
        "macd_hist":   safe(row["macd_hist"]),
        "ema_20":      safe(row.get("ema_20")),
        "ema_50":      safe(row.get("ema_50")),
        "ema_200":     safe(row.get("ema_200")),
        "atr":         safe(row["atr"]),
        "bb_upper":    safe(row["bb_upper"]),
        "bb_mid":      safe(row["bb_mid"]),
        "bb_lower":    safe(row["bb_lower"]),
        "bb_pct":      safe(row["bb_pct"]),
        "kc_squeeze":  int(row["kc_squeeze"]) if not pd.isna(row["kc_squeeze"]) else 0,
        "vwap":        safe(row["vwap"]),
        "adx":         safe(row["adx"]),
        "rvol":        safe(row["rvol"]),
        "stoch_k":     safe(row["stoch_k"]),
        "stoch_d":     safe(row["stoch_d"]),
        "obv":         safe(row["obv"]),
        "cci":         safe(row["cci"]),
        "williams_r":  safe(row["williams_r"]),
        "supertrend":  safe(row["supertrend"]),
        "supertrend_dir": int(row["supertrend_dir"]) if not pd.isna(row["supertrend_dir"]) else 0,
    }


if __name__ == "__main__":
    import asyncio, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from db.schema import init_db
    from data.fetcher import DataFetcher

    async def test():
        init_db()
        fetcher = DataFetcher("BTC/USDT", ["1h"], history_bars=100)
        data = await fetcher.load_all()
        await fetcher.close()
        df = data["1h"]
        print(f"Fetched {len(df)} bars")
        df = add_indicators(df)
        snap = get_snapshot(df)
        print("\nIndicator Snapshot (latest bar):")
        for k, v in snap.items():
            print(f"  {k:20s}: {v}")

    asyncio.run(test())
