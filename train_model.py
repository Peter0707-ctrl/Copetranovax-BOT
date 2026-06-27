"""
CopetraNova -- live_bot.py  (v7 -- trust the model)
=====================================================
Production signal bot for XAUUSD M15.

v7 philosophy: TRUST THE MODEL
  The RF + XGBoost dual vote trained on 92,875 candles IS the filter.
  When both models agree, that signal is real. Stop blocking it.

What was removed vs v6:
  - EMA hard block REMOVED  (ML already uses EMA as a feature)
  - News block windows REMOVED  (ATR spike gate handles this)
  - Cooldown after loss REMOVED  (reduces signal volume unnecessarily)
  - Duplicate close guard REMOVED  (real consecutive signals do happen)
  - Scorer threshold LOWERED to 1  (just need any one confirmation)

What is kept:
  - Dual ML vote  (RF + XGBoost must agree)
  - ATR gate  (only blocks truly flat or spiking markets)
  - SELL deceleration  (prevents selling into rising momentum)
  - BUY reversal engine  (catches oversold bounces)
  - In-memory signal ID  (prevents exact same signal twice)
  - Signal file writer  (MQL5 EA integration)

Expected result: 15-25 signals per active trading day
                 2-4 signals per active hour

Run:  python live_bot.py
Stop: Ctrl+C
"""

import os
import sys
import time
import joblib
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[CopetraNova] ERROR: pip install MetaTrader5")
    sys.exit(1)

try:
    from xgboost import XGBClassifier  # noqa
except ImportError:
    print("[CopetraNova] ERROR: pip install xgboost")
    sys.exit(1)

# -- paths ---------------------------------------------------------------------
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR   = os.path.join(BASE_DIR, "models")
LOG_DIR     = os.path.join(BASE_DIR, "data", "logs")
LOG_PATH    = os.path.join(LOG_DIR, "signals.csv")
SIGNAL_FILE = os.path.join(BASE_DIR, "data", "signal.txt")
RESULT_FILE = os.path.join(BASE_DIR, "data", "result.txt")
os.makedirs(LOG_DIR, exist_ok=True)

RF_PATH   = os.path.join(MODEL_DIR, "rf_brain.pkl")
XGB_PATH  = os.path.join(MODEL_DIR, "xgb_brain.pkl")
FEAT_PATH = os.path.join(MODEL_DIR, "feature_list.pkl")

# ==============================================================================
# CONFIG -- tuned for high volume + accuracy
# ==============================================================================

SYMBOL   = "XAUUSD"
PIP_SIZE = 0.1

# TP/SL by grade
TP_SL = {
    "STRONG": {"tp": 40, "sl": 15},
    "GOOD":   {"tp": 28, "sl": 12},
    "SOFT":   {"tp": 18, "sl": 10},
}

# ATR gate -- only block truly extreme conditions
# 10th percentile = market completely dead (no movement at all)
# 97th percentile = extreme news spike (spread widens dangerously)
ATR_MIN_PERCENTILE = 10
ATR_MAX_PERCENTILE = 97

# ML confidence floor -- 43% on a 3-class problem is real edge
MIN_CONFIDENCE = 0.43

# Scorer -- just 1 confirmation needed
# The dual ML vote IS the primary filter -- scorer just removes pure noise
MIN_SCORE_STRONG = 1
MIN_SCORE_GOOD   = 1
MIN_SCORE_SOFT   = 1

# SELL thresholds
RSI_OVERBOUGHT   = 72    # slightly lower than before for more SELL signals
RSI_OVERSOLD     = 30
ATR_MIN_FOR_SELL = 15

# Momentum reversal (SELL)
MOM_REVERSAL_DROP = 0.15   # 15% drop from peak (was 20%)
RSI_REVERSAL_MIN  = 68

# BUY reversal
RSI_BUY_REVERSAL_MAX  = 38   # slightly higher for more BUY reversals
MOM_BUY_REVERSAL_RISE = 0.15

BARS_NEEDED = 250

# in-memory state
_last_candle_key   = None
_fired_signal_ids  = set()


# ==============================================================================
# INDICATOR HELPERS
# ==============================================================================

def ema_s(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def atr_s(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr   = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def rsi_s(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr_pct_s(atr_series: pd.Series, window: int = 100) -> pd.Series:
    return atr_series.rolling(window).rank(pct=True) * 100


# ==============================================================================
# MT5 DATA FETCH
# ==============================================================================

def connect_mt5() -> bool:
    for attempt in range(3):
        if mt5.initialize():
            return True
        print(f"  Attempt {attempt+1} failed -- retrying...")
        time.sleep(2)
    print(f"  ERROR: {mt5.last_error()}")
    return False

def fetch_m15(n: int = BARS_NEEDED) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 1, n)
    if rates is None or len(rates) < 200:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)
    return df[["open", "high", "low", "close", "tick_volume"]]

def fetch_m5_context(n: int = 6) -> dict:
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, n)
    if rates is None or len(rates) < 3:
        return {"dir": "N/A", "mom": 0.0, "note": "M5 unavailable"}
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    m5_mom = df["close"].iloc[-1] - df["close"].iloc[-3]
    m5_dir = "UP" if m5_mom > 0 else "DOWN"
    last_d = "bullish" if (df["close"].iloc[-1] - df["open"].iloc[-1]) > 0 else "bearish"
    return {
        "dir":  m5_dir,
        "mom":  m5_mom,
        "note": f"M5 {m5_dir} ({m5_mom:+.2f}) | last candle {last_d}",
    }

def get_price() -> float | None:
    tick = mt5.symbol_info_tick(SYMBOL)
    return round(tick.bid, 2) if tick else None


# ==============================================================================
# FEATURE COMPUTATION
# ==============================================================================

def compute_features(df: pd.DataFrame, feature_cols: list) -> dict | None:
    try:
        c = df["close"]
        o = df["open"]
        h = df["high"]
        l = df["low"]

        _ema9   = ema_s(c, 9)
        _ema21  = ema_s(c, 21)
        _ema50  = ema_s(c, 50)
        _ema200 = ema_s(c, 200)
        _atr    = atr_s(df, 14)
        _rsi    = rsi_s(c, 14)
        _atr_p  = atr_pct_s(_atr, 100)
        _mom    = c - c.shift(5)
        _acc    = _mom - _mom.shift(1)

        rng        = h - l
        upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
        lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l
        body       = (c - o).abs()

        mom_now    = _mom.iloc[-1]
        mom_prev1  = _mom.iloc[-2]
        mom_peak   = max(_mom.iloc[-4], _mom.iloc[-3],
                         _mom.iloc[-2], _mom.iloc[-1])
        mom_trough = min(_mom.iloc[-4], _mom.iloc[-3],
                         _mom.iloc[-2], _mom.iloc[-1])

        return {
            # model features (must match feature_list.pkl)
            "body":           body.iloc[-1],
            "range":          rng.iloc[-1],
            "upper_wick":     upper_wick.iloc[-1],
            "lower_wick":     lower_wick.iloc[-1],
            "price_vs_ema50": (c - _ema50).iloc[-1],
            "momentum":       mom_now,
            "acceleration":   _acc.iloc[-1],
            "atr":            _atr.iloc[-1],
            "volatility":     _atr.iloc[-1] / max(c.iloc[-1], 0.001),
            "atr_pct":        _atr_p.iloc[-1],

            # auxiliary
            "_atr_pct":       _atr_p.iloc[-1],
            "_rsi":           _rsi.iloc[-1],
            "_rsi_prev1":     _rsi.iloc[-2],
            "_ema_gap_fast":  (_ema9  - _ema21).iloc[-1],
            "_ema_gap_slow":  (_ema50 - _ema200).iloc[-1],
            "_momentum":      mom_now,
            "_mom_prev1":     mom_prev1,
            "_mom_peak":      mom_peak,
            "_mom_trough":    mom_trough,
            "_acceleration":  _acc.iloc[-1],
            "_close":         c.iloc[-1],
            "_open":          o.iloc[-1],
            "_body":          body.iloc[-1],
            "_range":         rng.iloc[-1],
            "_upper_wick":    upper_wick.iloc[-1],
            "_lower_wick":    lower_wick.iloc[-1],
            "_prev_body":     body.iloc[-2],
            "_prev_open":     o.iloc[-2],
            "_prev_close":    c.iloc[-2],
        }
    except Exception as e:
        print(f"  [feature error] {e}")
        return None


# ==============================================================================
# LAYER 1 -- SCREENER (minimal -- only blocks extreme conditions)
# ==============================================================================

def screener(feat: dict) -> tuple:
    """
    Only two blocks:
      1. ATR too low  = market completely flat, no movement to trade
      2. ATR too high = dangerous news spike, spread unreliable
    No session filter -- 24 hours.
    No EMA filter -- ML model handles trend.
    No news time filter -- ATR handles this automatically.
    """
    atr_p = feat["_atr_pct"]

    if atr_p < ATR_MIN_PERCENTILE:
        return False, f"Market dead -- ATR {atr_p:.0f}th pct (min {ATR_MIN_PERCENTILE})"

    if atr_p > ATR_MAX_PERCENTILE:
        return False, f"Extreme spike -- ATR {atr_p:.0f}th pct (max {ATR_MAX_PERCENTILE})"

    return True, "OK"


# ==============================================================================
# LAYER 2 -- ML ENGINE (dual vote)
# ==============================================================================

def ml_vote(feat: dict, feature_cols: list, rf, xgb) -> tuple:
    """
    The primary signal filter.
    Both RF and XGBoost trained on 92,875 candles must agree.
    When they agree at 43%+ confidence on a 3-class problem, it is real.
    """
    try:
        X = np.array([[feat[f] for f in feature_cols]])
    except KeyError as e:
        print(f"  [ML error] Missing feature: {e}")
        return None, 0.0, None

    rf_pred   = rf.predict(X)[0]
    rf_proba  = rf.predict_proba(X)[0]
    xgb_pred  = xgb.predict(X)[0]
    xgb_proba = xgb.predict_proba(X)[0]

    if rf_pred != xgb_pred:
        return None, 0.0, None

    direction  = int(rf_pred)
    confidence = float((rf_proba[direction] + xgb_proba[direction]) / 2.0)

    if confidence < MIN_CONFIDENCE:
        return None, 0.0, None

    grade = ("STRONG" if confidence >= 0.68
             else "GOOD" if confidence >= 0.58
             else "SOFT")

    return direction, confidence, grade


# ==============================================================================
# LAYER 2b -- SELL ENGINE
# ==============================================================================

def sell_engine(feat: dict) -> tuple:
    """
    Method A: RSI overbought + momentum decelerating.
    Method B: RSI elevated + momentum reversal from peak.
    """
    rsi      = feat["_rsi"]
    rsi_p1   = feat["_rsi_prev1"]
    atr_p    = feat["_atr_pct"]
    mom      = feat["_momentum"]
    mom_p1   = feat["_mom_prev1"]
    mom_peak = feat["_mom_peak"]

    if rsi > RSI_OVERBOUGHT and atr_p > ATR_MIN_FOR_SELL and mom < mom_p1:
        conf  = min(0.50 + (rsi - RSI_OVERBOUGHT) / 80.0, 0.80)
        grade = "STRONG" if conf >= 0.68 else "GOOD" if conf >= 0.58 else "SOFT"
        return True, (f"RSI overbought ({rsi:.0f}>{RSI_OVERBOUGHT}) | "
                      f"momentum decelerating {mom:+.1f} < {mom_p1:+.1f}"), conf, grade

    if (rsi > RSI_REVERSAL_MIN
            and rsi < rsi_p1
            and mom_peak > 5
            and mom_p1 > 0
            and mom < mom_p1 * (1 - MOM_REVERSAL_DROP)
            and atr_p > ATR_MIN_FOR_SELL):
        conf  = min(0.52 + (rsi - RSI_REVERSAL_MIN) / 100.0, 0.72)
        grade = "STRONG" if conf >= 0.68 else "GOOD" if conf >= 0.58 else "SOFT"
        return True, (f"Momentum reversal | RSI {rsi:.0f} falling | "
                      f"mom {mom_peak:+.1f} to {mom:+.1f}"), conf, grade

    return False, "", 0.0, "SOFT"


# ==============================================================================
# LAYER 2c -- BUY REVERSAL ENGINE
# ==============================================================================

def buy_reversal_engine(feat: dict) -> tuple:
    """
    BUY when market is oversold and momentum starting to recover.
    RSI < 38, RSI rising, momentum recovering from deep trough.
    """
    rsi        = feat["_rsi"]
    rsi_p1     = feat["_rsi_prev1"]
    atr_p      = feat["_atr_pct"]
    mom        = feat["_momentum"]
    mom_p1     = feat["_mom_prev1"]
    mom_trough = feat["_mom_trough"]

    if (rsi < RSI_BUY_REVERSAL_MAX
            and rsi > rsi_p1
            and mom_trough < -5
            and mom_p1 < 0
            and mom > mom_p1
            and atr_p > ATR_MIN_PERCENTILE):

        recovery = abs(mom - mom_trough) / max(abs(mom_trough), 0.001)
        if recovery < MOM_BUY_REVERSAL_RISE:
            return False, "", 0.0, "SOFT"

        conf  = min(0.52 + (RSI_BUY_REVERSAL_MAX - rsi) / 100.0, 0.72)
        grade = "STRONG" if conf >= 0.68 else "GOOD" if conf >= 0.58 else "SOFT"
        return True, (f"BUY reversal | RSI {rsi:.0f} rising from {rsi_p1:.0f} | "
                      f"mom recovering {mom_trough:+.1f} to {mom:+.1f}"), conf, grade

    return False, "", 0.0, "SOFT"


# ==============================================================================
# LAYER 3 -- SCORER (minimal -- just 1 confirmation)
# ==============================================================================

def score_signal(direction: int, feat: dict, confidence: float) -> tuple:
    """
    Scorer with minimum threshold of 1.
    The ML dual vote is the real filter -- scorer just removes
    signals with zero supporting evidence (pure noise).

    Any single confirmation from price action passes the signal.
    """
    score    = 0
    evidence = []

    rsi    = feat["_rsi"]
    ema_f  = feat["_ema_gap_fast"]
    ema_sl = feat["_ema_gap_slow"]
    mom    = feat["_momentum"]
    acc    = feat["_acceleration"]
    body   = feat["_body"]
    rng    = feat["_range"]
    uw     = feat["_upper_wick"]
    lw     = feat["_lower_wick"]
    close  = feat["_close"]
    open_  = feat["_open"]
    p_body = feat["_prev_body"]
    p_open = feat["_prev_open"]
    p_cls  = feat["_prev_close"]

    if direction == 1:   # BUY confirmations
        if ema_f  > 0: score += 1; evidence.append("EMA fast bull")
        if ema_sl > 0: score += 1; evidence.append("EMA slow bull")
        if mom    > 0: score += 1; evidence.append(f"mom {mom:+.1f}")
        if acc    > 0: score += 1; evidence.append("accelerating")
        if rsi    < 65:score += 1; evidence.append(f"RSI {rsi:.0f} ok")
        if rsi    < RSI_OVERSOLD + 12:
            score += 1; evidence.append("RSI near oversold")
        if close  > open_: score += 1; evidence.append("bull candle")
        if lw > body * 0.35: score += 1; evidence.append("wick rejection")
        if rng > 0 and body / rng > 0.5: score += 1; evidence.append("strong body")
        if (close > open_ and p_cls < p_open and body > p_body * 0.6):
            score += 2; evidence.append("ENGULFING")

    elif direction == 2:   # SELL confirmations
        if ema_f  < 0: score += 1; evidence.append("EMA fast bear")
        if ema_sl < 0: score += 1; evidence.append("EMA slow bear")
        if mom    < 0: score += 1; evidence.append(f"mom {mom:+.1f}")
        if acc    < 0: score += 1; evidence.append("decelerating")
        if rsi    > 55:score += 1; evidence.append(f"RSI {rsi:.0f} high")
        if rsi    > RSI_OVERBOUGHT: score += 1; evidence.append("RSI overbought")
        if close  < open_: score += 1; evidence.append("bear candle")
        if uw > body * 0.35: score += 1; evidence.append("wick rejection")
        if rng > 0 and body / rng > 0.5: score += 1; evidence.append("strong body")
        if (close < open_ and p_cls > p_open and body > p_body * 0.6):
            score += 2; evidence.append("ENGULFING")

    threshold = MIN_SCORE_STRONG   # always 1 -- all grades same threshold
    passed    = score >= threshold
    breakdown = (f"score={score}/{threshold}  "
                 f"[{', '.join(evidence) if evidence else 'no extra confirm'}]")
    return score, breakdown, passed


# ==============================================================================
# SIGNAL ID (in-memory duplicate guard)
# ==============================================================================

def get_signal_id(direction: int, price: float, candle_time: datetime) -> str:
    dir_c    = "B" if direction == 1 else "S"
    time_str = candle_time.strftime("%Y%m%d%H%M")
    p_str    = str(int(price * 10))
    return f"{dir_c}_{time_str}_{p_str}"


# ==============================================================================
# SIGNAL OUTPUT
# ==============================================================================

def print_signal(direction: int, confidence: float, grade: str,
                 reason: str, feat: dict, price: float,
                 candle_time: datetime, m5_ctx: dict,
                 score: int, signal_id: str) -> None:

    tpsl    = TP_SL.get(grade, TP_SL["SOFT"])
    tp_pips = tpsl["tp"]
    sl_pips = tpsl["sl"]

    if direction == 1:
        tp_price = round(price + tp_pips * PIP_SIZE, 2)
        sl_price = round(price - sl_pips * PIP_SIZE, 2)
        dir_str  = "BUY  [LONG] "
        sym      = "^^"
    else:
        tp_price = round(price - tp_pips * PIP_SIZE, 2)
        sl_price = round(price + sl_pips * PIP_SIZE, 2)
        dir_str  = "SELL [SHORT]"
        sym      = "vv"

    now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rr      = round(tp_pips / sl_pips, 1)

    print("\n" + "=" * 60)
    print(f"  CopetraNova {sym}  |  {now_utc}")
    print("=" * 60)
    print(f"  Direction  :  {dir_str}")
    print(f"  Grade      :  {grade}  "
          f"({confidence*100:.1f}% conf | score {score})")
    print(f"  Candle     :  M15  {candle_time.strftime('%H:%M')}")
    print(f"  Entry      :  {price:.2f}")
    print("-" * 60)
    print(f"  Take Profit:  {tp_price:.2f}  (+{tp_pips} pips)")
    print(f"  Stop Loss  :  {sl_price:.2f}  (-{sl_pips} pips)")
    print(f"  Risk/Reward:  {rr} : 1")
    print("-" * 60)
    print(f"  RSI (14)   :  {feat['_rsi']:.1f}  (prev {feat['_rsi_prev1']:.1f})")
    print(f"  ATR pct    :  {feat['_atr_pct']:.0f}th percentile")
    print(f"  Momentum   :  {feat['_momentum']:+.1f}  (prev {feat['_mom_prev1']:+.1f})")
    print(f"  EMA fast   :  {feat['_ema_gap_fast']:+.2f}")
    print(f"  EMA slow   :  {feat['_ema_gap_slow']:+.2f}")
    print("-" * 60)
    print(f"  M5 context :  {m5_ctx['note']}")
    print(f"  Reason     :  {reason}")
    print(f"  Signal ID  :  {signal_id}")
    print("=" * 60)


def log_signal(direction: int, confidence: float, grade: str,
               reason: str, price: float, candle_time: datetime,
               m5_ctx: dict, score: int, feat: dict,
               signal_id: str) -> None:
    tpsl = TP_SL.get(grade, TP_SL["SOFT"])
    row  = {
        "datetime":   datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "candle":     candle_time.strftime("%Y-%m-%d %H:%M"),
        "signal_id":  signal_id,
        "direction":  "BUY" if direction == 1 else "SELL",
        "confidence": f"{confidence*100:.1f}",
        "grade":      grade,
        "score":      score,
        "price":      price,
        "tp_pips":    tpsl["tp"],
        "sl_pips":    tpsl["sl"],
        "rsi":        f"{feat['_rsi']:.1f}",
        "momentum":   f"{feat['_momentum']:+.1f}",
        "atr_pct":    f"{feat['_atr_pct']:.0f}",
        "ema_fast":   f"{feat['_ema_gap_fast']:+.2f}",
        "m5_context": m5_ctx["dir"],
        "reason":     reason,
        "outcome":    "OPEN",
    }
    df_row = pd.DataFrame([row])
    header = not os.path.exists(LOG_PATH)
    df_row.to_csv(LOG_PATH, mode="a", header=header, index=False)


def write_signal_file(direction: int, confidence: float, grade: str,
                      price: float, feat: dict, signal_id: str) -> None:
    tpsl     = TP_SL.get(grade, TP_SL["SOFT"])
    dir_name = "BUY" if direction == 1 else "SELL"
    now_str  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines    = [
        "DIRECTION="  + dir_name,
        "GRADE="      + grade,
        "CONFIDENCE=" + f"{confidence*100:.1f}",
        "PRICE="      + f"{price:.2f}",
        "TP="         + str(tpsl["tp"]),
        "SL="         + str(tpsl["sl"]),
        "RSI="        + f"{feat['_rsi']:.1f}",
        "MOMENTUM="   + f"{feat['_momentum']:+.1f}",
        "ATR_PCT="    + f"{feat['_atr_pct']:.0f}",
        "SIGNAL_ID="  + signal_id,
        "TIMESTAMP="  + now_str,
        "STATUS=NEW",
    ]
    try:
        with open(SIGNAL_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("  [SIGNAL FILE] Written -> signal.txt")
    except Exception as e:
        print(f"  [SIGNAL FILE] Error: {e}")


# ==============================================================================
# MODEL LOADER
# ==============================================================================

def load_models() -> tuple:
    for path in [RF_PATH, XGB_PATH, FEAT_PATH]:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"[CopetraNova] Not found: {path}\n"
                f"  Run train_model.py first."
            )
    rf           = joblib.load(RF_PATH)
    xgb          = joblib.load(XGB_PATH)
    feature_cols = joblib.load(FEAT_PATH)
    print(f"  RF model   : {rf.n_estimators} trees  depth={rf.max_depth}")
    print(f"  XGB model  : loaded")
    print(f"  Features   : {feature_cols}")
    return rf, xgb, feature_cols


# ==============================================================================
# CANDLE TIMER
# ==============================================================================

def wait_for_next_m15() -> datetime:
    while True:
        now  = datetime.now(tz=timezone.utc)
        mins = now.minute
        secs = now.second

        if (mins % 15 == 0) and (secs < 2):
            return now

        mins_into = mins % 15
        secs_left = (15 - mins_into) * 60 - secs

        if   secs_left > 120: time.sleep(60)
        elif secs_left > 30:  time.sleep(15)
        elif secs_left > 5:   time.sleep(2)
        else:                  time.sleep(0.3)


# ==============================================================================
# MAIN LOOP
# ==============================================================================

def run() -> None:
    global _last_candle_key

    print("\n" + "=" * 60)
    print("  CopetraNova -- live_bot.py  (v7)")
    print("  XAUUSD M15  |  Trust the model -- high volume signals")
    print("=" * 60 + "\n")

    print("[1/3] Connecting to MetaTrader 5...")
    if not connect_mt5():
        sys.exit(1)

    sym = mt5.symbol_info(SYMBOL)
    if sym is None:
        print(f"  ERROR: {SYMBOL} not found.")
        mt5.shutdown()
        sys.exit(1)
    if not sym.visible:
        mt5.symbol_select(SYMBOL, True)

    acct = mt5.account_info()
    print(f"  Connected  : {acct.server}  (account {acct.login})")
    print(f"  Balance    : {acct.currency} {acct.balance:,.2f}")
    print(f"  Spread     : {sym.spread} points\n")

    print("[2/3] Loading models...")
    try:
        rf, xgb, feature_cols = load_models()
    except FileNotFoundError as e:
        print(e)
        mt5.shutdown()
        sys.exit(1)

    print(f"\n[3/3] Bot is LIVE\n")
    print(f"  Session      : 24 hours")
    print(f"  ATR gate     : dead below {ATR_MIN_PERCENTILE}th | "
          f"spike above {ATR_MAX_PERCENTILE}th")
    print(f"  Min conf     : {MIN_CONFIDENCE*100:.0f}%")
    print(f"  Scorer       : 1 confirmation needed (ML is the main filter)")
    print(f"  SELL trigger : RSI>{RSI_OVERBOUGHT} + decel | reversal")
    print(f"  BUY reversal : RSI<{RSI_BUY_REVERSAL_MAX} + bounce")
    print(f"  M5           : context only")
    print(f"  Log          : {LOG_PATH}")
    print(f"\n  Waiting for next M15 candle...  (Ctrl+C to stop)\n")

    total_signals  = 0
    total_blocked  = 0
    total_nosig    = 0
    total_filtered = 0

    while True:
        try:
            candle_time = wait_for_next_m15()
            now_str     = candle_time.strftime("%H:%M:%S")

            # duplicate candle guard (time based)
            candle_key = candle_time.strftime("%Y%m%d%H%M")
            if candle_key == _last_candle_key:
                time.sleep(2)
                continue
            _last_candle_key = candle_key

            print(f"\n[{now_str} UTC]  New M15 candle --",
                  end="", flush=True)

            time.sleep(2)

            # fetch
            df15 = fetch_m15(BARS_NEEDED)
            if df15 is None or len(df15) < 200:
                n = len(df15) if df15 is not None else 0
                print(f" only {n} bars. Skipping.")
                time.sleep(30)
                continue

            price = get_price()
            if price is None:
                print(" no price. Skipping.")
                time.sleep(30)
                continue

            feat = compute_features(df15, feature_cols)
            if feat is None:
                print(" feature error. Skipping.")
                time.sleep(30)
                continue

            print(f" done.", flush=True)

            # LAYER 1: screener (minimal)
            ok, msg = screener(feat)
            if not ok:
                total_blocked += 1
                print(f"  [BLOCKED]    {msg}")
                continue

            print(f"  [SCREENER]   Pass | "
                  f"ATR {feat['_atr_pct']:.0f}th | "
                  f"RSI {feat['_rsi']:.0f} | "
                  f"mom {feat['_momentum']:+.1f} | "
                  f"EMA {feat['_ema_gap_fast']:+.2f}")

            # LAYER 2: ML vote
            ml_dir, ml_conf, ml_grade = ml_vote(feat, feature_cols, rf, xgb)

            direction  = None
            confidence = 0.0
            grade      = None
            reason     = ""

            if ml_dir == 1:
                direction  = 1
                confidence = ml_conf
                grade      = ml_grade
                print(f"  [ML VOTE]    BUY agreed | "
                      f"conf={ml_conf*100:.1f}% | grade={ml_grade}")

                # TREND + MOMENTUM GATE
                # Block BUY when both overall trend (EMA slow) and
                # momentum are negative -- price is in a downtrend.
                # EMA fast alone ticking up does not make a real BUY.
                if (feat["_ema_gap_slow"] < 0
                        and feat["_momentum"] < -3):
                    total_filtered += 1
                    print(f"  [BLOCKED]    BUY blocked -- EMA slow "
                          f"{feat['_ema_gap_slow']:+.2f} bearish "
                          f"AND momentum {feat['_momentum']:+.1f} "
                          f"negative -- downtrend active")
                    direction = None
                    continue

            else:
                # SELL engine
                sell_ok, sell_rsn, sell_conf, sell_grade = sell_engine(feat)
                if sell_ok:
                    direction  = 2
                    confidence = sell_conf
                    grade      = sell_grade
                    reason     = sell_rsn
                    method     = ("RSI overbought"
                                  if feat["_rsi"] > RSI_OVERBOUGHT
                                  else "reversal")
                    print(f"  [SELL ENGINE] SELL -- {method} | "
                          f"conf={sell_conf*100:.1f}% | grade={sell_grade}")
                else:
                    # BUY reversal engine
                    buy_ok, buy_rsn, buy_conf, buy_grade = buy_reversal_engine(feat)
                    if buy_ok:
                        direction  = 1
                        confidence = buy_conf
                        grade      = buy_grade
                        reason     = buy_rsn
                        print(f"  [BUY REVERSAL] BUY -- oversold bounce | "
                              f"conf={buy_conf*100:.1f}% | grade={buy_grade}")
                    else:
                        total_nosig += 1
                        ml_lbl = {0:"HOLD",1:"BUY",2:"SELL"}.get(ml_dir,"NONE")
                        print(f"  [NO SIGNAL]  ML={ml_lbl} "
                              f"conf={ml_conf*100:.1f}% | "
                              f"RSI={feat['_rsi']:.0f} | "
                              f"mom={feat['_momentum']:+.1f}")
                        continue

            # LAYER 3: scorer (threshold = 1, just blocks pure noise)
            score, breakdown, passed = score_signal(
                direction, feat, confidence)
            print(f"  [SCORER]     {breakdown}")

            if not passed:
                total_filtered += 1
                print(f"  [FILTERED]   Score 0 -- no confirming evidence at all")
                continue

            # build BUY reason if not set by reversal engine
            if direction == 1 and not reason:
                parts = []
                if feat["_ema_gap_fast"] > 0:  parts.append("EMA bull")
                if feat["_momentum"]    > 0:   parts.append(f"mom {feat['_momentum']:+.1f}")
                if feat["_rsi"]         < 60:  parts.append(f"RSI {feat['_rsi']:.0f}")
                if feat["_lower_wick"]  > feat["_body"] * 0.35:
                    parts.append("wick rejection")
                reason = (" | ".join(parts) if parts else "ML pattern") \
                         + f"  [{breakdown}]"

            # in-memory signal ID guard
            sig_id = get_signal_id(direction, price, candle_time)
            if sig_id in _fired_signal_ids:
                print(f"  [DUPLICATE]  {sig_id} already fired")
                continue
            _fired_signal_ids.add(sig_id)

            # M5 context
            m5_ctx = fetch_m5_context(6)
            print(f"  [M5 CTX]     {m5_ctx['note']}  (info only)")

            # FIRE
            total_signals += 1
            print_signal(direction, confidence, grade, reason,
                         feat, price, candle_time, m5_ctx,
                         score, sig_id)
            log_signal(direction, confidence, grade, reason,
                       price, candle_time, m5_ctx, score, feat, sig_id)
            write_signal_file(direction, confidence, grade,
                              price, feat, sig_id)

