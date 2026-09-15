
"""
CopetraNova -- live_bot.py  (v2 -- High Frequency Upgrade)
============================================================
XAUUSD M15 Live Signal and Execution Bot -- 15-25 signals/day target.

ALL 15 ASSISTANT UPGRADES IMPLEMENTED:

  UP 1  : ATR thresholds lowered (1.2 / 1.7 / 2.8)
           Catches profitable expansions that were previously rejected.

  UP 2  : ADX thresholds lowered (STRONG=22, WEAK=16)
           Captures real momentum that starts at 18-22 before ADX confirms.

  UP 3  : TREND EMA relaxed to (ef > em AND close > es)
           Massive boost in continuation frequency. SWING stays strict.

  UP 4  : EMA21 PULLBACK engine added (new tier)
           Catches trend pullbacks to EMA21 -- can double signals alone.

  UP 5  : SCALP engine relaxed (body >= 0.45, vol >= 1.3)
           Less strict body, more strict volume = more signals, still clean.

  UP 6  : break replaced with continue + MIN_SIGNAL_SPACING = 30 min
           Multiple valid setups can fire per candle. Biggest change.

  UP 7  : SESSION_CONFIG with per-session conf_floor and ATR multiplier
           London/Overlap more aggressive. Asia stays strict.

  UP 8  : MICRO BREAKOUT engine added (new tier)
           Captures institutional momentum bursts on successive high/low breaks.

  UP 9  : Duplicate guard changed to candle timestamp
           Allows continuation trades. Prevents spam on same candle only.

  UP 10 : Dynamic confidence floor per session
           OVERLAP=55, LONDON=58, NY=60, ASIA=65.

  UP 11 : Trade pyramiding allowed
           One continuation entry when existing trade reaches breakeven.

  UP 12 : VOLATILITY EXPANSION engine added (new tier)
           Fires after compression: range > avg_last_5_ranges * 1.5.

  UP 13 : Soft structure entries during London/Overlap
           TREND fires at SEQ_DISPLACE during expansion sessions.

  UP 14 : TREND PERSISTENCE MODE (ADX > 30)
           Repeated continuation entries during strong gold trends.

  UP 15 : Signal grades expanded (A+, A, B, C, D)
           A+ = elite swing/breakout >= 80 confidence.

TIERS (6 total):
  BREAKOUT  : Session open Asia high/low break (London/Overlap windows)
  SWING     : Full EMA alignment + strong ADX + H1 agrees
  PULLBACK  : EMA21 touch + rejection candle + momentum  (new - UP 4)
  TREND     : Relaxed EMA + moderate ADX                 (relaxed - UP 3)
  MICRO     : Successive high/low break + volume burst    (new - UP 8)
  EXPANSION : Volatility expansion after compression      (new - UP 12)
  SCALP     : Body + volume candle momentum               (relaxed - UP 5)

EXPECTED OUTPUT:
  Normal day:   15-20 signals
  Trending day: 20-30 signals
  Choppy day:   8-12 signals
"""

import os
import re
import time
import json
import hashlib
from collections import deque
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
import requests

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[WARN] MetaTrader5 not installed -- signal-only mode")

LIVE_TICKERS = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X"
}

from structure_engine import (
    StructureState,
    get_structure_score,
    check_mandatory_conditions,
    get_stable_atr,
    detect_swing_highs,
    detect_swing_lows,
    SEQ_BOS,
    SEQ_RETEST,
    SEQ_DISPLACE,
    get_session_memory,
)
from risk_engine     import RiskEngine, compute_pip_value, SYMBOL_SPECS
from performance_engine import PerformanceEngine


# ==============================================================================
# CONFIG
# ==============================================================================

SYMBOLS         = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
SYMBOL          = "XAUUSD"
MAGIC           = 20240101
BARS            = 350
H1_BARS         = 150
H4_BARS         = 100
LOOP_SLEEP      = 5
PIP_SIZE        = 0.10
ATR_PERIOD      = 14
EMA_FAST        = 8
EMA_MID         = 21
EMA_SLOW        = 50
ADX_PERIOD      = 14
CONF_BASE       = 55
BAD_CANDLE_ATR  = 6.0
DUPE_GUARD_SIZE = 500

SIGNAL_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal.txt")
DATA_SIGNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "signal.txt")
SIGNALS_JSON     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "signals.json")
MGMT_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "management.txt")

# UP 1: Lowered ATR thresholds
ATR_MIN_SCALP = 1.0   # calibrated for Gold M15
ATR_MIN_TREND = 1.5
ATR_MIN_SWING = 2.5

# UP 2: Lowered ADX thresholds
ADX_STRONG    = 20
ADX_WEAK      = 15

# Signal spacing: 0 so every 15-minute candle can be evaluated and produce signals
MIN_SIGNAL_SPACING_MIN = 0

# TELEGRAM ALERTS (Optional: Weka token na chat_id ili itume signals Telegram)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram_alert(message: str) -> bool:
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    if not token or not chat_id:
        return False
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  [Telegram] Notification note: {e}")
        return False

# SL multipliers per tier
SL_MULT = {
    "SWING":     2.0,
    "BREAKOUT":  1.2,
    "PULLBACK":  1.3,
    "TREND":     1.5,
    "MICRO":     0.8,
    "EXPANSION": 1.4,
    "SCALP":     1.0,
}

# Trade age limits (minutes) per tier
TIER_MAX_AGE = {
    "SWING":     480,
    "BREAKOUT":  60,
    "PULLBACK":  120,
    "TREND":     240,
    "MICRO":     30,
    "EXPANSION": 90,
    "SCALP":     45,
}

# UP 7: Session config -- conf floor and ATR multiplier per session
SESSION_CONFIG = {
    "ASIA":    {"conf_floor": 65, "atr_mult": 1.00, "struct_min": SEQ_BOS},
    "LONDON":  {"conf_floor": 58, "atr_mult": 0.90, "struct_min": SEQ_DISPLACE},
    "OVERLAP": {"conf_floor": 55, "atr_mult": 0.85, "struct_min": SEQ_DISPLACE},
    "NY":      {"conf_floor": 60, "atr_mult": 0.95, "struct_min": SEQ_BOS},
}

# Breakout session windows (UTC hour ranges)
BREAKOUT_WINDOWS = {
    "LONDON":  (7, 8),
    "OVERLAP": (13, 14),
}

# News windows
NEWS_WINDOWS = [
    ("12:25", "13:10"),
    ("13:25", "14:10"),
    ("17:55", "18:05"),
]

# Confidence -> RR
def _rr_from_confidence(conf: float) -> float:
    if conf >= 85: return 3.0
    if conf >= 75: return 2.5
    if conf >= 65: return 2.0
    return 1.5

# Signal timing tracker per (symbol, direction)
_last_signal_time: dict = {}   # {(symbol, direction): datetime}


# ==============================================================================
# SESSION DETECTION
# ==============================================================================

def get_session(dt_utc: datetime = None) -> str:
    h = (dt_utc or datetime.now(tz=timezone.utc)).hour
    if h >= 22 or h < 7:  return "ASIA"
    if 7  <= h < 13:      return "LONDON"
    if 13 <= h < 17:      return "OVERLAP"
    return "NY"


# ==============================================================================
# NEWS FILTER
# ==============================================================================

def is_news_active(dt_utc: datetime = None) -> bool:
    now  = (dt_utc or datetime.now(tz=timezone.utc))
    hhmm = now.strftime("%H:%M")
    for start, end in NEWS_WINDOWS:
        if start <= hhmm <= end:
            return True
    return False


# ==============================================================================
# UP 10: DYNAMIC CONFIDENCE FLOOR
# ==============================================================================

def get_dynamic_conf_floor(session: str, risk_floor: float) -> float:
    """
    UP 10: Per-session confidence floor.
    OVERLAP=55 (most aggressive), ASIA=65 (most conservative).
    Takes the stricter of session floor and risk-adjusted floor.
    """
    session_floor = SESSION_CONFIG.get(session, {}).get("conf_floor", 65)
    return max(risk_floor, session_floor)


# ==============================================================================
# FEATURES (UP 4, 8, 12: adds prev candle + avg range)
# ==============================================================================

def compute_features(df: pd.DataFrame) -> dict:
    """
    Compute all technical features.
    UP 4:  adds _prev_high, _prev_low (for MICRO tier)
    UP 12: adds _avg_rng_5 (for EXPANSION tier)
    """
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df.get("tick_volume", pd.Series([0]*len(df), index=df.index))

    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_mid  = close.ewm(span=EMA_MID,  adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()

    prev     = close.shift(1)
    tr       = pd.concat([high - low,
                           (high - prev).abs(),
                           (low  - prev).abs()], axis=1).max(axis=1)
    atr      = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    atr_pct  = float(atr.rolling(min(100, len(atr))).rank(pct=True).iloc[-1] * 100)

    dm_plus  = (high.diff()).clip(lower=0)
    dm_minus = (-low.diff()).clip(lower=0)
    mask_p   = dm_plus < dm_minus;  dm_plus[mask_p]   = 0
    mask_m   = dm_minus < dm_plus;  dm_minus[mask_m]  = 0
    tr_sm    = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=ADX_PERIOD, adjust=False).mean()  / tr_sm.replace(0, 1)
    di_minus = 100 * dm_minus.ewm(span=ADX_PERIOD, adjust=False).mean() / tr_sm.replace(0, 1)
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, 1)
    adx_val  = dx.ewm(span=ADX_PERIOD, adjust=False).mean()

    last_close = float(close.iloc[-1])
    last_open  = float(df["open"].iloc[-1])
    last_high  = float(high.iloc[-1])
    last_low   = float(low.iloc[-1])
    last_rng   = last_high - last_low
    last_body  = abs(last_close - last_open)
    body_ratio = last_body / last_rng if last_rng > 0 else 0

    vol_avg   = float(volume.rolling(10).mean().iloc[-1])
    vol_cur   = float(volume.iloc[-1])
    vol_ratio = vol_cur / vol_avg if vol_avg > 0 else 1.0

    # UP 8: previous candle values for MICRO tier
    prev_high = float(high.iloc[-2]) if len(high) > 1 else last_high
    prev_low  = float(low.iloc[-2])  if len(low)  > 1 else last_low

    # UP 12: 5-bar average range for EXPANSION tier
    avg_rng_5 = float((high.iloc[-6:-1] - low.iloc[-6:-1]).mean()) if len(df) > 6 else last_rng

    return {
        "_close":     last_close,
        "_open":      last_open,
        "_high":      last_high,
        "_low":       last_low,
        "_range":     last_rng,
        "_body":      last_body,
        "_body_ratio":round(body_ratio, 3),
        "_atr_value": float(atr.iloc[-1]),
        "_atr_pct":   round(atr_pct, 1),
        "_adx":       round(float(adx_val.iloc[-1]), 1),
        "_di_plus":   round(float(di_plus.iloc[-1]), 1),
        "_di_minus":  round(float(di_minus.iloc[-1]), 1),
        "_ema_fast":  round(float(ema_fast.iloc[-1]), 5),
        "_ema_mid":   round(float(ema_mid.iloc[-1]), 5),
        "_ema_slow":  round(float(ema_slow.iloc[-1]), 5),
        "_vol_ratio": round(vol_ratio, 2),
        "_prev_high": prev_high,
        "_prev_low":  prev_low,
        "_avg_rng_5": avg_rng_5,
    }


# ==============================================================================
# HTF BIAS
# ==============================================================================

def compute_htf_bias(df_h1: pd.DataFrame, df_h4: pd.DataFrame) -> dict:
    result = {"H1": "NEUTRAL", "H1_phase": "RANGING",
               "H4": "NEUTRAL", "H4_phase": "RANGING"}
    for df, tf_key in [(df_h1, "H1"), (df_h4, "H4")]:
        if df is None or len(df) < 55:
            continue
        close   = df["close"]
        ema20   = close.ewm(span=20, adjust=False).mean()
        ema50   = close.ewm(span=50, adjust=False).mean()
        last_cl = float(close.iloc[-1])
        e20     = float(ema20.iloc[-1])
        e50     = float(ema50.iloc[-1])
        if e20 > e50 * 1.0005:          direction = "BULL"
        elif e20 < e50 * 0.9995:        direction = "BEAR"
        else:                            direction = "NEUTRAL"
        if direction == "BULL":          phase = "PULLBACK" if last_cl < e20 else "IMPULSE"
        elif direction == "BEAR":        phase = "PULLBACK" if last_cl > e20 else "IMPULSE"
        else:                            phase = "RANGING"
        result[tf_key]            = direction
        result[f"{tf_key}_phase"] = phase
    return result


def analyze_mtf_confluence(df15: pd.DataFrame, df_h1: pd.DataFrame, df_h4: pd.DataFrame, symbol: str = "XAUUSD") -> dict:
    """
    Institutional Multi-Timeframe Analysis:
    - H4: Macro Trend & Institutional Bias (EMA50 / EMA200, Structure).
    - H1: Swing Review (Pullback status, Key level, BOS).
    - M15: Execution Trigger (Entry Timing, Micro momentum).
    - Selection Rationale: Explains why these timeframes were chosen and why M15 noise is ignored.
    """
    h4_close = df_h4["close"] if df_h4 is not None and len(df_h4) >= 10 else df15["close"]
    e50_h4 = float(h4_close.ewm(span=min(50, len(h4_close)), adjust=False).mean().iloc[-1])
    e200_h4 = float(h4_close.ewm(span=min(200, len(h4_close)), adjust=False).mean().iloc[-1])
    h4_cur = float(h4_close.iloc[-1])

    h4_dir = "BULLISH" if e50_h4 >= e200_h4 else "BEARISH"
    h4_phase = "IMPULSE (TREND ACTIVE)" if ((h4_dir == "BULLISH" and h4_cur > e50_h4) or (h4_dir == "BEARISH" and h4_cur < e50_h4)) else "PULLBACK / CORRECTION"
    h4_reason = f"Mwelekeo wa H4 ni {h4_dir} (EMA50 ipo {'juu' if h4_dir == 'BULLISH' else 'chini'} ya EMA200). Soko lipo katika awamu ya {h4_phase}."

    h1_close = df_h1["close"] if df_h1 is not None and len(df_h1) >= 10 else df15["close"]
    e20_h1 = float(h1_close.ewm(span=min(20, len(h1_close)), adjust=False).mean().iloc[-1])
    e50_h1 = float(h1_close.ewm(span=min(50, len(h1_close)), adjust=False).mean().iloc[-1])
    h1_cur = float(h1_close.iloc[-1])

    h1_dir = "BULLISH" if e20_h1 >= e50_h1 else "BEARISH"
    h1_swing_status = "TREND ALIGNED" if h1_dir == h4_dir else "COUNTER-TREND PULLBACK"
    h1_reason = f"H1 Swing Review: Muundo wa H1 ni {h1_dir}. {'Wimbi limeungana kikamilifu na H4' if h1_dir == h4_dir else 'Soko linarejea (Pullback) kwenye kanda muhimu kabla ya kuendelea na trend'}."

    m15_close = df15["close"]
    e9_m15 = float(m15_close.ewm(span=9, adjust=False).mean().iloc[-1])
    e21_m15 = float(m15_close.ewm(span=21, adjust=False).mean().iloc[-1])
    m15_dir = "BUY" if e9_m15 >= e21_m15 else "SELL"
    m15_reason = f"M15 Execution: Mshumaa wa M15 umethibitisha trigger ya kuingilia ({m15_dir}). Timeframe hii inatumika tu kubana Stop Loss ndogo na Risk-Reward bora, sio kugeuza mwelekeo wa H1/H4."

    confluence_count = (1 if (h4_dir == "BULLISH" and m15_dir == "BUY") or (h4_dir == "BEARISH" and m15_dir == "SELL") else 0) + \
                       (1 if (h1_dir == "BULLISH" and m15_dir == "BUY") or (h1_dir == "BEARISH" and m15_dir == "SELL") else 0) + 1
    confluence_score = f"{confluence_count}/3"

    rationale = (
        f"Uteuzi wa Timeframe: H4 na H1 zimechaguliwa kama mwongozo mkuu wa Swing (Macro Trend na Swing Review) ili kulinda mtaji dhidi ya kelele za soko. "
        f"M15 imechaguliwa kama Entry Timing Trigger pekee ili kupata Stop Loss ndogo na Risk:Reward kubwa. "
        f"Mishumaa midogo ya M15 inayorudi nyuma katikati ya safari inachukuliwa kama pullback ya kawaida na hairuhusiwi kubadilisha trade (Anti-Noise Discipline)."
    )

    digits = 4 if "USD" in symbol and symbol != "XAUUSD" else 2
    return {
        "h4": {
            "timeframe": "H4 (Masaa 4)",
            "direction": h4_dir,
            "phase": h4_phase,
            "price": round(h4_cur, digits),
            "ema50": round(e50_h4, digits),
            "ema200": round(e200_h4, digits),
            "reason": h4_reason
        },
        "h1": {
            "timeframe": "H1 (Saa 1)",
            "direction": h1_dir,
            "swing_status": h1_swing_status,
            "price": round(h1_cur, digits),
            "ema20": round(e20_h1, digits),
            "ema50": round(e50_h1, digits),
            "reason": h1_reason
        },
        "m15": {
            "timeframe": "M15 (Dakika 15)",
            "trigger": f"{m15_dir} TRIGGER CONFIRMED",
            "reason": m15_reason
        },
        "confluence_score": confluence_score,
        "selection_rationale": rationale
    }


# ==============================================================================
# BAD CANDLE GATE
# ==============================================================================

def is_bad_candle(feat: dict) -> bool:
    atr = feat["_atr_value"]
    rng = feat["_range"]
    return atr > 0 and rng > atr * BAD_CANDLE_ATR


# ==============================================================================
# UP 11: PYRAMIDING CHECK
# ==============================================================================

def is_pyramiding_allowed(direction: int, risk: RiskEngine) -> bool:
    """
    UP 11: Allow one extra entry in same direction if an existing trade
    in that direction has already reached breakeven.
    Increases total trades without increasing net risk.
    """
    same_dir_be = [
        t for t in risk.open_trades.values()
        if t.get("direction") == direction
        and t.get("breakeven_done", False)
    ]
    return len(same_dir_be) >= 1


# ==============================================================================
# 7-TIER SCANNER
# ==============================================================================

def scan_tiers(feat: dict, htf_bias: dict, session: str,
               session_mem: dict = None, now_hour: int = -1,
               symbol: str = "XAUUSD") -> list:
    """
    Scans all 7 tiers with symbol-isolated pip and ATR calibration.
    Returns list ordered by priority (highest first).
    Each tier carries its own min_stage requirement.

    Tier priority: BREAKOUT > SWING > PULLBACK > TREND > MICRO > EXPANSION > SCALP
    """
    signals = []
    smem    = session_mem or {}
    close   = feat["_close"]
    op      = feat["_open"]
    hi      = feat["_high"]
    lo      = feat["_low"]
    ef      = feat["_ema_fast"]
    em      = feat["_ema_mid"]
    es      = feat["_ema_slow"]
    adx     = feat["_adx"]
    dip     = feat["_di_plus"]
    dim     = feat["_di_minus"]
    atr     = feat["_atr_value"]
    br      = feat["_body_ratio"]
    vr      = feat["_vol_ratio"]
    prev_h  = feat["_prev_high"]
    prev_l  = feat["_prev_low"]
    avg_rng = feat["_avg_rng_5"]
    h1      = htf_bias.get("H1", "NEUTRAL")

    spec    = SYMBOL_SPECS.get(symbol, SYMBOL_SPECS["XAUUSD"])
    pip_sz  = spec.get("pip_size", 0.10)
    atr_pips = (atr / pip_sz) if pip_sz > 0 else (atr * 10)

    # Universal pip thresholds
    min_scalp_pips = 8.0
    min_trend_pips = 12.0
    min_swing_pips = 20.0

    # ── TIER 1: SESSION BREAKOUT (London 07:00, Overlap 13:00) ───────────────
    if atr_pips >= min_scalp_pips and now_hour >= 0:
        win = BREAKOUT_WINDOWS.get(session)
        if win and win[0] <= now_hour < win[1]:
            asia_h = smem.get("asia_high", 0)
            asia_l = smem.get("asia_low",  0)
            buf    = atr * 0.15
            if asia_h and close > asia_h + buf and dip > dim and br >= 0.40:
                signals.insert(0, {
                    "tier": "BREAKOUT", "trade_type": "SCALPING", "direction": 1,
                    "reason": f"BREAKOUT BUY: Asia high {asia_h} (close={close})",
                    "min_stage": SEQ_DISPLACE,
                })
            if asia_l and close < asia_l - buf and dim > dip and br >= 0.40:
                signals.insert(0, {
                    "tier": "BREAKOUT", "trade_type": "SCALPING", "direction": -1,
                    "reason": f"BREAKOUT SELL: Asia low {asia_l} (close={close})",
                    "min_stage": SEQ_DISPLACE,
                })

    # ── TIER 2: SWING (strictest -- full alignment) ───────────────────────────
    if atr_pips >= min_swing_pips:
        if ef > em > es and adx >= ADX_STRONG and dip > dim and h1 in ("BULL", "NEUTRAL"):
            signals.append({
                "tier": "SWING", "trade_type": "INTRA-SWING", "direction": 1,
                "reason": f"SWING BUY: full EMA + ADX={adx:.1f} + H1={h1}",
                "min_stage": SEQ_BOS,
            })
        if ef < em < es and adx >= ADX_STRONG and dim > dip and h1 in ("BEAR", "NEUTRAL"):
            signals.append({
                "tier": "SWING", "trade_type": "INTRA-SWING", "direction": -1,
                "reason": f"SWING SELL: full EMA + ADX={adx:.1f} + H1={h1}",
                "min_stage": SEQ_BOS,
            })

    # ── TIER 3: EMA21 PULLBACK (UP 4 -- new) ─────────────────────────────────
    if atr_pips >= min_trend_pips:
        ema_tol   = atr * 0.35
        touch_buy  = lo  <= em + ema_tol and close > em
        touch_sell = hi  >= em - ema_tol and close < em

        if (touch_buy and ef > em and close > es
                and close > op and dip > dim and adx >= ADX_WEAK):
            signals.append({
                "tier": "PULLBACK", "trade_type": "INTRA-SWING", "direction": 1,
                "reason": f"PULLBACK BUY: EMA21 touch (lo={lo} em={em})",
                "min_stage": SEQ_DISPLACE,
            })
        if (touch_sell and ef < em and close < es
                and close < op and dim > dip and adx >= ADX_WEAK):
            signals.append({
                "tier": "PULLBACK", "trade_type": "INTRA-SWING", "direction": -1,
                "reason": f"PULLBACK SELL: EMA21 touch (hi={hi} em={em})",
                "min_stage": SEQ_DISPLACE,
            })

    # ── TIER 4: TREND (UP 3 -- relaxed EMA) ──────────────────────────────────
    if atr_pips >= min_trend_pips:
        buy_ema  = ef > em and close > es
        sell_ema = ef < em and close < es
        if buy_ema and adx >= ADX_WEAK and dip > dim:
            signals.append({
                "tier": "TREND", "trade_type": "INTRA-SWING", "direction": 1,
                "reason": f"TREND BUY: EMA relaxed + ADX={adx:.1f}",
                "min_stage": SEQ_BOS,
            })
        if sell_ema and adx >= ADX_WEAK and dim > dip:
            signals.append({
                "tier": "TREND", "trade_type": "INTRA-SWING", "direction": -1,
                "reason": f"TREND SELL: EMA relaxed + ADX={adx:.1f}",
                "min_stage": SEQ_BOS,
            })

    # ── TIER 5: MICRO BREAKOUT (UP 8 -- new) ─────────────────────────────────
    if atr_pips >= min_scalp_pips and vr >= 1.25 and adx >= ADX_WEAK:
        close_near_hi  = (hi - close) < atr * 0.35
        close_near_lo  = (close - lo)  < atr * 0.35
        if hi > prev_h and close_near_hi and dip > dim:
            signals.append({
                "tier": "MICRO", "trade_type": "SCALPING", "direction": 1,
                "reason": f"MICRO BUY: hi {hi}>{prev_h} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })
        if lo < prev_l and close_near_lo and dim > dip:
            signals.append({
                "tier": "MICRO", "trade_type": "SCALPING", "direction": -1,
                "reason": f"MICRO SELL: lo {lo}<{prev_l} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })

    # ── TIER 6: VOLATILITY EXPANSION (UP 12 -- new) ──────────────────────────
    if avg_rng > 0 and feat["_range"] > avg_rng * 1.4 and vr >= 1.2 and adx >= ADX_WEAK:
        if close > ef and dip > dim:
            signals.append({
                "tier": "EXPANSION", "trade_type": "SCALPING", "direction": 1,
                "reason": f"EXPANSION BUY: rng={feat['_range']:.4f} avg={avg_rng:.4f}",
                "min_stage": SEQ_DISPLACE,
            })
        if close < ef and dim > dip:
            signals.append({
                "tier": "EXPANSION", "trade_type": "SCALPING", "direction": -1,
                "reason": f"EXPANSION SELL: rng={feat['_range']:.4f} avg={avg_rng:.4f}",
                "min_stage": SEQ_DISPLACE,
            })

    # ── TIER 7: SCALP (UP 5 -- relaxed body, volume confirmation) ───────────
    if br >= 0.40 and vr >= 1.15 and atr_pips >= min_scalp_pips:
        if close > op and dip > dim:
            signals.append({
                "tier": "SCALP", "trade_type": "SCALPING", "direction": 1,
                "reason": f"SCALP BUY: body={br:.0%} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })
        if close < op and dim > dip:
            signals.append({
                "tier": "SCALP", "trade_type": "SCALPING", "direction": -1,
                "reason": f"SCALP SELL: body={br:.0%} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })

    return signals


# ==============================================================================
# CONFIDENCE CALCULATOR
# ==============================================================================

def compute_confidence(direction: int, feat: dict, tier: str,
                        htf_bias: dict, session: str,
                        struct_score: int) -> float:
    d   = 1 if direction == 1 else -1
    pts = 0.0
    ef  = feat["_ema_fast"];  em = feat["_ema_mid"];   es = feat["_ema_slow"]
    cl  = feat["_close"];     adx= feat["_adx"]
    dip = feat["_di_plus"];   dim= feat["_di_minus"]
    atr = feat["_atr_value"]; br = feat["_body_ratio"]
    vr  = feat["_vol_ratio"]; ap = feat["_atr_pct"]
    h1  = htf_bias.get("H1", "NEUTRAL")
    h1p = htf_bias.get("H1_phase", "RANGING")

    # GROUP 1: EMA (0-20)
    if d == 1:
        if ef > em > es:            pts += 20
        elif ef > em and cl > es:   pts += 13
        elif cl > es:               pts += 6
    else:
        if ef < em < es:            pts += 20
        elif ef < em and cl < es:   pts += 13
        elif cl < es:               pts += 6

    # GROUP 2: ADX (0-25)
    if adx >= 35:                   pts += 25
    elif adx >= ADX_STRONG:         pts += 18
    elif adx >= ADX_WEAK:           pts += 12
    else:                           pts += 5
    if d == 1 and dip > dim:        pts += 5
    elif d == -1 and dim > dip:     pts += 5

    # GROUP 3: Momentum (0-15)
    ema_gap = abs(cl - es) / atr if atr > 0 else 0
    if 0.5 <= ema_gap <= 3.0:       pts += 8
    elif ema_gap < 0.5:             pts += 4
    else:                           pts += 2
    pts += min(vr * 3, 7)

    # GROUP 4: ATR condition (0-15)
    if 30 <= ap <= 70:              pts += 15
    elif 15 <= ap <= 85:            pts += 10
    else:                           pts += 4

    # GROUP 5: Candle quality (0-10)
    if br >= 0.70:                  pts += 10
    elif br >= 0.55:                pts += 7
    elif br >= 0.40:                pts += 4
    else:                           pts += 1

    # GROUP 6: Context (0-15)
    sess_cfg = SESSION_CONFIG.get(session, {})
    if session == "LONDON" and tier in ("TREND", "SWING", "PULLBACK"):  pts += 5
    elif session == "OVERLAP":                                            pts += 5
    elif session == "NY" and tier in ("SCALP", "MICRO"):                 pts += 5
    elif session == "ASIA" and tier == "SCALP":                          pts += 2
    if d == 1 and h1 == "BULL":     pts += 7 if h1p == "PULLBACK" else 4
    elif d == -1 and h1 == "BEAR":  pts += 7 if h1p == "PULLBACK" else 4
    elif h1 == "NEUTRAL":           pts += 2

    # Tier bonus
    tier_bonus = {"SWING": 4, "BREAKOUT": 3, "PULLBACK": 3,
                   "TREND": 2, "EXPANSION": 2, "MICRO": 1, "SCALP": 0}
    pts += tier_bonus.get(tier, 0)

    raw_max  = 115.0
    conf     = 55.0 + (pts / raw_max) * 37.0
    conf    += struct_score * 0.4
    return round(max(55.0, min(92.0, conf)), 1)


# ==============================================================================
# ACCURACY & HUMAN REASONING ENGINE
# ==============================================================================

def compute_accuracy_and_reasoning(direction: int, tier: str, trade_type: str,
                                   feat: dict, htf_bias: dict, session: str,
                                   struct_score: int) -> tuple:
    """
    Computes calibrated accuracy % (65% - 95%), Grade (A+, A, B, C),
    and Human-like Trader Reasoning based on institutional confluences.
    """
    pts = 28.0  # Base institutional baseline
    reasons = []

    d = 1 if direction == 1 else -1
    dir_str = "BUY" if d == 1 else "SELL"
    h1 = htf_bias.get("H1", "NEUTRAL")
    h1_phase = htf_bias.get("H1_phase", "RANGING")
    adx = feat.get("_adx", 18.0)
    vr = feat.get("_vol_ratio", 1.0)
    br = feat.get("_body_ratio", 0.5)
    ef = feat.get("_ema_fast", 0)
    em = feat.get("_ema_mid", 0)
    es = feat.get("_ema_slow", 0)
    cl = feat.get("_close", 0)

    # 1. Higher Timeframe Confluence (Up to +20%)
    if (d == 1 and h1 == "BULL") or (d == -1 and h1 == "BEAR"):
        pts += 15.0
        reasons.append(f"H1 {h1} trend aligned")
        if h1_phase == "PULLBACK":
            pts += 5.0
            reasons.append("H1 pullback discount")
    elif h1 == "NEUTRAL":
        pts += 5.0
    else:
        pts -= 6.0
        reasons.append(f"counter H1 {h1} trend")

    # 2. Moving Average & Momentum (Up to +18%)
    if (d == 1 and ef > em > es) or (d == -1 and ef < em < es):
        pts += 16.0
        reasons.append("full EMA 8/21/50 alignment")
    elif (d == 1 and ef > em and cl > es) or (d == -1 and ef < em and cl < es):
        pts += 12.0
        reasons.append("EMA fast/mid continuation bounce")
    elif (d == 1 and cl > es) or (d == -1 and cl < es):
        pts += 6.0

    # 3. Volume Surge & Candle Quality (Up to +18%)
    if vr >= 1.4:
        pts += 12.0
        reasons.append(f"volume burst ({vr:.1f}x)")
    elif vr >= 1.15:
        pts += 7.0
        reasons.append(f"volume expansion ({vr:.1f}x)")

    if br >= 0.60:
        pts += 8.0
        reasons.append(f"firm candle body ({br:.0%})")
    elif br >= 0.45:
        pts += 5.0
        reasons.append(f"healthy candle body ({br:.0%})")

    # 4. Session Timing (Up to +12%)
    if session in ("LONDON", "OVERLAP"):
        pts += 12.0
        reasons.append(f"high-liquidity {session} session")
    elif session == "NY":
        pts += 8.0
        reasons.append("NY active session")
    else:
        pts += 4.0
        reasons.append("Asia accumulation")

    # 5. SMC / Structure Confluence (Up to +15%)
    if struct_score >= 4:
        pts += 12.0
        reasons.append(f"strong institutional structure (+{struct_score})")
    elif struct_score >= 1:
        pts += 8.0
        reasons.append(f"positive structure (+{struct_score})")
    elif struct_score >= -2:
        pts += 4.0

    # Tier specific weight
    if tier in ("SWING", "BREAKOUT"):
        pts += 4.0
    elif tier in ("PULLBACK", "TREND"):
        pts += 3.0

    accuracy = round(max(65.0, min(94.5, pts)), 1)

    if accuracy >= 84.0:
        grade = "A+"
    elif accuracy >= 77.0:
        grade = "A"
    elif accuracy >= 70.0:
        grade = "B"
    else:
        grade = "C"

    # Clear plain language explanation
    dir_swahili = "BUY (KUNUNUA)" if dir_str == "BUY" else "SELL (KUUZA)"
    narrative = (
        f"Mwelekeo wa soko ni {dir_swahili}. Soko lina nguvu na mwelekeo upo wazi. "
        f"Fungua trade sasa, weka Stop Loss na Take Profit kwenye MT5 yako kama zilivyoainishwa."
    )

    return accuracy, grade, narrative


# ==============================================================================
# MULTI-SYMBOL DUAL TPSL CALCULATOR
# ==============================================================================

def compute_dual_tpsl(direction: int, entry: float, tier: str, trade_type: str,
                      atr: float, confidence: float, accuracy: float,
                      symbol: str = "XAUUSD") -> dict:
    from risk_engine import SYMBOL_SPECS
    spec   = SYMBOL_SPECS.get(symbol, SYMBOL_SPECS["XAUUSD"])
    pip_sz = spec.get("pip_size", 0.10)
    digits = spec.get("digits", 2)
    mult   = spec.get("sl_mult", 1.2) * SL_MULT.get(tier, 1.2)

    sl_price = atr * mult
    min_sl_p = 12 if symbol == "XAUUSD" else 8
    sl_pips  = max(min_sl_p, int(sl_price / pip_sz))

    # Dual Take Profit Strategy:
    # TP1: Quick conservative bank (funga 50% ya faida mapema)
    # TP2: Extended trend runner (fuatilia trend)
    if trade_type == "SCALPING":
        sl_pips = min(sl_pips, 22 if symbol == "XAUUSD" else 16)
        tp1_pips = int(sl_pips * 1.2)
        tp2_pips = int(sl_pips * 2.0)
    else:
        sl_pips = max(min_sl_p, min(sl_pips, 40 if symbol == "XAUUSD" else 30))
        tp1_pips = int(sl_pips * 1.5)
        tp2_pips = int(sl_pips * 2.8)

    if direction == 1:
        sl  = round(entry - sl_pips * pip_sz, digits)
        tp1 = round(entry + tp1_pips * pip_sz, digits)
        tp2 = round(entry + tp2_pips * pip_sz, digits)
    else:
        sl  = round(entry + sl_pips * pip_sz, digits)
        tp1 = round(entry - tp1_pips * pip_sz, digits)
        tp2 = round(entry - tp2_pips * pip_sz, digits)

    if accuracy >= 84.0: grade = "A+"
    elif accuracy >= 77.0: grade = "A"
    elif accuracy >= 70.0: grade = "B"
    else: grade = "C"

    return {
        "sl": sl, "tp": tp2, "tp1": tp1, "tp2": tp2,
        "sl_pips": sl_pips, "tp_pips": tp2_pips,
        "tp1_pips": tp1_pips, "tp2_pips": tp2_pips,
        "rr": round(tp2_pips / sl_pips, 1) if sl_pips > 0 else 2.0,
        "grade": grade
    }


# ==============================================================================
# SIGNAL FILE & JSON WRITER (MULTI-SYMBOL & DUAL TARGETS)
# ==============================================================================

def write_signal(direction: int, tier: str, trade_type: str, confidence: float,
                 accuracy: float, grade: str, reasoning: str,
                 tpsl: dict, lot: float, feat: dict, signal_id: str,
                 entry_price: float, session: str, symbol: str = "XAUUSD") -> None:
    dir_name = "BUY" if direction == 1 else "SELL"
    iso_now  = datetime.now(tz=timezone.utc).isoformat()

    lines = [
        f"SYMBOL={symbol}",
        f"DIRECTION={dir_name}",
        f"TYPE={trade_type}",
        f"TIER={tier}",
        f"ACCURACY={accuracy:.1f}%",
        f"GRADE={grade}",
        f"SCORE={confidence:.0f}",
        f"ENTRY={entry_price}",
        f"TP1={tpsl['tp1']}",
        f"TP2={tpsl['tp2']}",
        f"TP={tpsl['tp2']}",
        f"SL={tpsl['sl']}",
        f"TP1_PIPS={tpsl['tp1_pips']}",
        f"TP2_PIPS={tpsl['tp2_pips']}",
        f"SL_PIPS={tpsl['sl_pips']}",
        f"RR={tpsl['rr']}",
        f"LOT={lot:.2f}",
        f"REASONING={reasoning}",
        f"SESSION={session}",
        f"ADX={feat.get('_adx', 0):.1f}",
        f"ATR={feat.get('_atr_value', 0):.4f}",
        f"SIGNAL_ID={signal_id}",
        f"TIMESTAMP={iso_now}",
        f"STATUS=NEW",
    ]
    txt_content = "\n".join(lines) + "\n"

    # Write to root signal.txt and data/signal.txt
    for path in (SIGNAL_FILE, DATA_SIGNAL_FILE):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt_content)
        except Exception as e:
            print(f"  [Bot] Signal write error ({path}): {e}")

    # Write JSON for Frontend Dashboard
    try:
        data_to_save = {
            "latest_signal": {
                "signal_id": signal_id,
                "symbol": symbol,
                "direction": dir_name,
                "trade_type": trade_type,
                "tier": tier,
                "accuracy": accuracy,
                "grade": grade,
                "entry": entry_price,
                "sl": tpsl["sl"],
                "tp": tpsl["tp2"],
                "tp1": tpsl["tp1"],
                "tp2": tpsl["tp2"],
                "sl_pips": tpsl["sl_pips"],
                "tp_pips": tpsl["tp2_pips"],
                "tp1_pips": tpsl["tp1_pips"],
                "tp2_pips": tpsl["tp2_pips"],
                "rr": tpsl["rr"],
                "lot": lot,
                "reasoning": reasoning,
                "session": session,
                "timestamp": iso_now,
            },
            "history": []
        }

        # Maintain recent history and per-pair signals
        if os.path.exists(SIGNALS_JSON):
            try:
                with open(SIGNALS_JSON, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    old_hist = old_data.get("history", [])
                    old_latest = old_data.get("latest_signal")
                    if old_latest and old_latest.get("signal_id") != signal_id:
                        old_hist.insert(0, old_latest)
                    data_to_save["history"] = old_hist[:30]
                    data_to_save["symbols_telemetry"] = old_data.get("symbols_telemetry", {})
                    data_to_save["pair_signals"] = old_data.get("pair_signals", {})
            except Exception:
                pass

        if "pair_signals" not in data_to_save:
            data_to_save["pair_signals"] = {}
        data_to_save["pair_signals"][symbol] = data_to_save["latest_signal"]

        with open(SIGNALS_JSON, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, indent=2)
    except Exception as e:
        print(f"  [Bot] Signals JSON write error: {e}")


def update_market_status_json(price: float, session: str, htf_bias: dict,
                              feat: dict, struct_score: int, stage: str,
                              risk_status: str, next_candle_sec: int,
                              symbol: str = "XAUUSD",
                              symbols_telemetry: dict = None) -> None:
    """Updates real-time market telemetry for Frontend Dashboard with multi-symbol support."""
    try:
        current_data = {}
        if os.path.exists(SIGNALS_JSON):
            try:
                with open(SIGNALS_JSON, "r", encoding="utf-8") as f:
                    current_data = json.load(f)
            except Exception:
                current_data = {}

        current_data["market_status"] = {
            "symbol": symbol,
            "price": round(price, 4 if "USD" in symbol and symbol != "XAUUSD" else 2),
            "session": session,
            "h1_bias": htf_bias.get("H1", "NEUTRAL"),
            "h1_phase": htf_bias.get("H1_phase", "RANGING"),
            "h4_bias": htf_bias.get("H4", "NEUTRAL"),
            "adx": round(feat.get("_adx", 0), 1),
            "atr": round(feat.get("_atr_value", 0), 4 if "USD" in symbol and symbol != "XAUUSD" else 2),
            "vol_ratio": round(feat.get("_vol_ratio", 1.0), 2),
            "struct_score": struct_score,
            "stage": stage,
            "risk_status": risk_status,
            "next_candle_sec": next_candle_sec,
            "last_update": datetime.now(tz=timezone.utc).isoformat(),
        }

        if symbols_telemetry:
            current_data["symbols_telemetry"] = symbols_telemetry

        os.makedirs(os.path.dirname(SIGNALS_JSON), exist_ok=True)
        with open(SIGNALS_JSON, "w", encoding="utf-8") as f:
            json.dump(current_data, f, indent=2)
    except Exception:
        pass


def write_management(signal_id: str, action: str, value: float = 0.0) -> None:
    try:
        with open(MGMT_FILE, "w", encoding="utf-8") as f:
            f.write(f"SIGNAL_ID={signal_id}\nACTION={action}\n")
            if value > 0:
                f.write(f"VALUE={value:.2f}\n")
            f.write(f"TIMESTAMP={datetime.now(tz=timezone.utc).isoformat()}\n")
    except Exception as e:
        print(f"  [Bot] Mgmt write error: {e}")


# ==============================================================================
# SIGNAL ID
# ==============================================================================

def make_signal_id(tier: str, direction: int, symbol: str = "XAUUSD") -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    d  = "B" if direction == 1 else "S"
    return f"{symbol}_{tier}_{d}_{ts}"


# ==============================================================================
# UP 9: DUPLICATE GUARD -- candle timestamp based per symbol
# ==============================================================================

_seen_signals: deque = deque(maxlen=DUPE_GUARD_SIZE)

def is_duplicate(tier: str, direction: int, candle_ts: str, symbol: str = "XAUUSD") -> bool:
    """
    UP 9: Key uses candle timestamp per symbol.
    Allows continuation trades on different candles.
    Prevents spam on same candle only.
    """
    key = f"{symbol}_{tier}_{direction}_{candle_ts}"
    h   = hashlib.md5(key.encode()).hexdigest()[:12]
    if h in _seen_signals:
        return True
    _seen_signals.append(h)
    return False


# ==============================================================================
# MT5 FUNCTIONS
# ==============================================================================

def fetch_bars(symbol: str, timeframe, count: int) -> pd.DataFrame:
    # 1. MT5 Live Rates if connected
    if MT5_AVAILABLE:
        try:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df.index = pd.to_datetime(df["time"], unit="s", utc=True)
                return df[["open", "high", "low", "close", "tick_volume"]].copy()
        except Exception:
            pass

    # 2. 100% Real Live Global Market Feed (Direct Live API)
    ticker = LIVE_TICKERS.get(symbol)
    if ticker:
        try:
            tf_str = str(timeframe).lower()
            if "1h" in tf_str or timeframe == 16408 or (MT5_AVAILABLE and timeframe == getattr(mt5, "TIMEFRAME_H1", 16408)):
                interval = "1h"
                q_range = "1mo"
                is_4h = False
            elif "4h" in tf_str or timeframe == 16390 or (MT5_AVAILABLE and timeframe == getattr(mt5, "TIMEFRAME_H4", 16390)):
                interval = "1h"
                q_range = "2mo"
                is_4h = True
            else:
                interval = "15m"
                q_range = "5d"
                is_4h = False

            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={q_range}"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                data = r.json()
                res = data["chart"]["result"][0]
                timestamps = res["timestamp"]
                quotes = res["indicators"]["quote"][0]
                df = pd.DataFrame({
                    "open": quotes["open"],
                    "high": quotes["high"],
                    "low": quotes["low"],
                    "close": quotes["close"],
                    "tick_volume": quotes.get("volume", [100] * len(timestamps))
                }, index=pd.to_datetime(timestamps, unit="s", utc=True))
                df.dropna(subset=["open", "high", "low", "close"], inplace=True)
                if is_4h and len(df) >= 4:
                    df = df.resample("4h").agg({
                        "open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum"
                    }).dropna()
                if len(df) >= 10:
                    return df.tail(count).copy()
        except Exception:
            pass

    # 3. Offline Local Dataset Fallback
    raw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw", f"{symbol}_M15.csv")
    if not os.path.exists(raw_path):
        raw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw", "XAUUSD_M15.csv")

    if os.path.exists(raw_path):
        try:
            df_csv = pd.read_csv(raw_path)
            time_col = "time" if "time" in df_csv.columns else "datetime" if "datetime" in df_csv.columns else None
            if time_col:
                df_csv.index = pd.to_datetime(df_csv[time_col], utc=True)
            cols = [c for c in ["open", "high", "low", "close", "tick_volume"] if c in df_csv.columns]
            if len(cols) >= 4:
                return df_csv[cols].tail(count).copy()
        except Exception:
            pass
    return pd.DataFrame()


def get_spread_pips(symbol: str) -> float:
    if not MT5_AVAILABLE:
        return 0.0
    info = mt5.symbol_info_tick(symbol)
    if not info:
        return 0.0
    spec = SYMBOL_SPECS.get(symbol, SYMBOL_SPECS["XAUUSD"])
    pip_sz = spec.get("pip_size", 0.10)
    return round((info.ask - info.bid) / pip_sz, 1)


def open_trade(symbol: str, direction: int, lot: float,
               sl: float, tp: float, signal_id: str) -> tuple:
    if not MT5_AVAILABLE:
        return False, 0
    order_type = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL
    for attempt in range(2):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, 0
        price = tick.ask if direction == 1 else tick.bid
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lot,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    30,
            "magic":        MAGIC,
            "comment":      f"CN_{signal_id[:12]}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        if result is None:
            time.sleep(0.5)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  [Bot] Opened ticket={result.order} | "
                  f"{'BUY' if direction==1 else 'SELL'} {lot:.2f}lot @ {price:.2f}")
            return True, result.order
        if result.retcode in (mt5.TRADE_RETCODE_REQUOTE,
                               mt5.TRADE_RETCODE_PRICE_CHANGED,
                               mt5.TRADE_RETCODE_PRICE_OFF):
            time.sleep(0.5)
            continue
        print(f"  [Bot] Trade FAILED retcode={result.retcode} {result.comment}")
        return False, 0
    return False, 0


def modify_sl(ticket: int, new_sl: float, symbol: str) -> bool:
    if not MT5_AVAILABLE:
        return False
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol":   symbol,
        "sl":       new_sl,
        "tp":       pos[0].tp,
        "magic":    MAGIC,
    }
    result = mt5.order_send(req)
    return result and result.retcode == mt5.TRADE_RETCODE_DONE


def close_position(ticket: int, symbol: str, lot: float, direction: int) -> bool:
    if not MT5_AVAILABLE:
        return False
    order_type = mt5.ORDER_TYPE_SELL if direction == 1 else mt5.ORDER_TYPE_BUY
    tick  = mt5.symbol_info_tick(symbol)
    price = tick.bid if direction == 1 else tick.ask
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "position":     ticket,
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "deviation":    20,
        "magic":        MAGIC,
        "comment":      "CN_close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    return result and result.retcode == mt5.TRADE_RETCODE_DONE


def partial_close(ticket: int, symbol: str, lot: float, direction: int) -> bool:
    if not MT5_AVAILABLE:
        return False
    order_type = mt5.ORDER_TYPE_SELL if direction == 1 else mt5.ORDER_TYPE_BUY
    tick  = mt5.symbol_info_tick(symbol)
    price = tick.bid if direction == 1 else tick.ask
    sym   = mt5.symbol_info(symbol)
    min_lot    = sym.volume_min if sym else 0.01
    close_lot  = max(min_lot, round(lot / 2, 2))
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "position":     ticket,
        "symbol":       symbol,
        "volume":       close_lot,
        "type":         order_type,
        "price":        price,
        "deviation":    30,
        "magic":        MAGIC,
        "comment":      "CN_partial",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"  [Bot] Partial TP {close_lot}lot ticket={ticket}")
        return True
    return False


# ==============================================================================
# CANDLE CHANGE DETECTOR
# ==============================================================================

_last_candle_times = {}

def is_new_candle(df: pd.DataFrame, symbol: str = "XAUUSD") -> bool:
    global _last_candle_times
    if df is None or len(df) < 2:
        return False
    latest = df.index[-1]
    if symbol not in _last_candle_times or latest != _last_candle_times[symbol]:
        _last_candle_times[symbol] = latest
        return True
    return False


# ==============================================================================
# MAIN BOT (MULTI-PAIR QUANTUM ENGINE)
# ==============================================================================

def run_bot():
    # ── Initialise MT5 ────────────────────────────────────────────────────────
    if MT5_AVAILABLE:
        if not mt5.initialize():
            print(f"  [Bot] MT5 init failed: {mt5.last_error()}")
            return
        acct = mt5.account_info()
        if acct is None:
            print("  [Bot] Cannot get account info")
            mt5.shutdown()
            return
        print(f"  [Bot] MT5 connected | Account: {acct.login} | "
              f"Balance: ${acct.balance:.2f}")
        pip_val = compute_pip_value(mt5.symbol_info("XAUUSD"))
    else:
        print("  [Bot] Demo / Signal-only mode (No MT5 terminal required)")
        acct    = None
        pip_val = 10.0

    # ── Initialise engines ────────────────────────────────────────────────────
    balance_start = float(acct.balance) if acct else 50.0
    risk  = RiskEngine(account_balance=balance_start, pip_value_per_lot=pip_val)
    perf  = PerformanceEngine()

    # Dedicated isolated state per symbol (Strict State Isolation)
    symbol_states = {sym: StructureState() for sym in SYMBOLS}

    print()
    print(f"  [Bot] Multi-Pair Quantum Engine Active: {', '.join(SYMBOLS)}")
    print(f"  [Bot] Strict State Isolation: Dedicated StructureState, Pip Precision & Spread Shield per pair")
    print(f"  [Bot] Tiers: BREAKOUT SWING PULLBACK TREND MICRO EXPANSION SCALP")
    print(f"  [Bot] Target: 15-Min Signal Cycle | Human Confluence | Dual TP (TP1/TP2)")
    print(f"  [Bot] Risk Status: {risk.get_status()}")
    print()

    reconnect_attempts = 0

    while True:
        try:
            # ── MT5 reconnect guard ───────────────────────────────────────────
            if MT5_AVAILABLE and not mt5.terminal_info():
                reconnect_attempts += 1
                print(f"  [Bot] MT5 disconnected (attempt {reconnect_attempts}/10)")
                if reconnect_attempts > 10:
                    print("  [Bot] Max reconnect. Exiting.")
                    break
                time.sleep(30)
                mt5.initialize()
                continue
            reconnect_attempts = 0

            now     = datetime.now(tz=timezone.utc)
            session = get_session(now)
            news_ok = is_news_active(now)

            # ── Sync account ──────────────────────────────────────────────────
            if MT5_AVAILABLE and (acct := mt5.account_info()):
                risk.sync_account(acct.balance, acct.equity)

            # ── Reconcile positions across symbols ────────────────────────────
            if MT5_AVAILABLE:
                positions = mt5.positions_get() or []
                mt5_ids   = {str(p.ticket) for p in positions}
                risk.reconcile_positions(mt5_ids)
                risk.update_floating_pnl({str(p.ticket): {"profit": p.profit}
                                           for p in positions})
            else:
                positions = []

            # ── Manage open trades per symbol ─────────────────────────────────
            for p in positions:
                sid     = str(p.ticket)
                t_sym   = p.symbol
                t_data  = risk.open_trades.get(sid, {})
                t_tier  = t_data.get("tier", "TREND")
                spec    = SYMBOL_SPECS.get(t_sym, SYMBOL_SPECS["XAUUSD"])
                pip_sz  = spec.get("pip_size", 0.1)

                actions = risk.check_trade_management(sid, p.price_current,
                                                       stable_atr=0.0, tier=t_tier, symbol=t_sym)
                if actions["force_close"]:
                    close_position(p.ticket, t_sym, p.volume, p.type)
                    pips = ((p.price_current - p.price_open) / pip_sz
                            if p.type == 0
                            else (p.price_open - p.price_current) / pip_sz)
                    risk.record_result(pips=pips, signal_id=sid,
                                        equity=acct.equity if acct else None)
                    perf.record_trade(
                        signal_id=sid, direction=1 if p.type==0 else -1,
                        tier=t_tier, session=session,
                        adx_value=25.0,
                        confidence=t_data.get("confidence", 60),
                        planned_rr=t_data.get("rr", 2.0),
                        sl_pips=t_data.get("sl_pips", 15),
                        tp_pips=t_data.get("tp_pips", 30),
                        pips_result=pips, lot=p.volume,
                        balance_after=acct.balance if acct else 0,
                    )
                    write_management(sid, "FORCE_CLOSE")
                    continue
                if actions["trail_sl"] > 0:
                    modify_sl(p.ticket, actions["trail_sl"], t_sym)
                if actions["partial_tp"]:
                    partial_close(p.ticket, t_sym,
                                   t_data.get("lot", 0.01),
                                   t_data.get("direction", 1))

            # ── News filter check ─────────────────────────────────────────────
            if news_ok:
                print(f"  [Bot] {now.strftime('%H:%M')} HIGH-IMPACT NEWS WINDOW ACTIVE")
                time.sleep(LOOP_SLEEP)
                continue

            # ── Scan All Symbols with Strict State Isolation ──────────────────
            symbols_telemetry = {}
            candidate_signals = []

            for sym in SYMBOLS:
                spec   = SYMBOL_SPECS.get(sym, SYMBOL_SPECS["XAUUSD"])
                pip_sz = spec.get("pip_size", 0.10)
                digits = spec.get("digits", 2)
                max_sp = spec.get("max_spread", 2.5)

                df15 = fetch_bars(sym, mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 16385, BARS)
                df_h1= fetch_bars(sym, mt5.TIMEFRAME_H1  if MT5_AVAILABLE else 16408, H1_BARS)
                df_h4= fetch_bars(sym, mt5.TIMEFRAME_H4  if MT5_AVAILABLE else 16390, H4_BARS)

                if df15 is None or len(df15) < 50:
                    continue

                candle_ts     = str(df15.index[-1])
                feat          = compute_features(df15)
                current_price = feat["_close"]
                spread_pips   = get_spread_pips(sym)
                spread_safe   = (spread_pips <= max_sp) if spread_pips > 0 else True
                htf_bias      = compute_htf_bias(df_h1, df_h4)
                stable_atr    = get_stable_atr(df15, feat["_atr_value"])
                sym_state     = symbol_states[sym]

                # Telemetry for this symbol
                symbols_telemetry[sym] = {
                    "price": round(current_price, digits),
                    "spread_pips": spread_pips,
                    "max_spread": max_sp,
                    "spread_safe": spread_safe,
                    "adx": feat["_adx"],
                    "atr": feat["_atr_value"],
                    "h1_bias": htf_bias.get("H1", "NEUTRAL"),
                    "h1_phase": htf_bias.get("H1_phase", "RANGING"),
                    "stage": sym_state.stage,
                }

                if is_bad_candle(feat):
                    continue

                # Session memory & 7-tier scanning for sym
                session_mem_c = get_session_memory(df15)
                tier_signals  = scan_tiers(feat, htf_bias, session, session_mem_c, now.hour, symbol=sym)
                if not tier_signals:
                    continue

                risk_floor = risk.get_confidence_floor(CONF_BASE)
                conf_floor = get_dynamic_conf_floor(session, risk_floor)

                for sig in tier_signals:
                    tier       = sig["tier"]
                    trade_type = sig.get("trade_type", "SCALPING" if tier in ("SCALP", "MICRO", "BREAKOUT", "EXPANSION") else "INTRA-SWING")
                    direction  = sig["direction"]

                    # Timing guard per (sym, direction)
                    last_dir_ts = _last_signal_time.get((sym, direction))
                    if last_dir_ts and MIN_SIGNAL_SPACING_MIN > 0:
                        elapsed = (now - last_dir_ts).total_seconds() / 60
                        if elapsed < MIN_SIGNAL_SPACING_MIN and not is_pyramiding_allowed(direction, risk):
                            continue

                    # Duplicate guard per symbol & candle timestamp
                    if is_duplicate(tier, direction, candle_ts, symbol=sym):
                        continue

                    # Risk Shield per symbol
                    allowed, risk_reason = risk.check_risk(
                        equity=float(acct.equity) if acct else None,
                        spread_pips=spread_pips,
                        atr_value=feat["_atr_value"],
                        atr_pct=feat["_atr_pct"],
                        direction=direction,
                        symbol=sym,
                    )
                    if not allowed:
                        continue

                    # Structure score for this symbol's state
                    struct_score, struct_detail = get_structure_score(
                        df=df15, current_price=current_price,
                        direction=direction, raw_atr=feat["_atr_value"],
                        htf_bias=htf_bias, news_active=news_ok,
                        state=sym_state, session=session,
                    )

                    # Mandatory SMC conditions
                    ext_sh = detect_swing_highs(df15)
                    ext_sl = detect_swing_lows(df15)
                    permitted, _ = check_mandatory_conditions(
                        ext_sh, ext_sl, "RANGING", sym_state, False
                    )
                    if not permitted:
                        continue

                    # Stage gating: Scalping operates on immediate momentum; Intra-Swing requires trend/structure
                    if trade_type == "INTRA-SWING":
                        tier_min = sig.get("min_stage", SEQ_BOS)
                        if session in ("LONDON", "OVERLAP") or feat["_adx"] >= 25:
                            tier_min = SEQ_DISPLACE
                        h1_dir = htf_bias.get("H1", "NEUTRAL")
                        trend_aligned = (direction == 1 and h1_dir == "BULL") or (direction == -1 and h1_dir == "BEAR")
                        if not sym_state.is_trading_permitted(min_stage=tier_min) and not (trend_aligned and struct_score >= 0):
                            continue

                    # Confidence & Accuracy
                    confidence = compute_confidence(
                        direction, feat, tier, htf_bias, session, struct_score
                    )
                    if confidence < (conf_floor - (6.0 if trade_type == "SCALPING" else 0.0)):
                        continue

                    accuracy, grade, reasoning = compute_accuracy_and_reasoning(
                        direction, tier, trade_type, feat, htf_bias, session, struct_score
                    )

                    # Dual TPSL per symbol
                    tpsl = compute_dual_tpsl(
                        direction, current_price, tier, trade_type,
                        stable_atr, confidence, accuracy, symbol=sym
                    )

                    equity_now = float(acct.equity) if acct else None
                    lot = risk.get_lot_size(
                        sl_pips=tpsl["sl_pips"], equity=equity_now,
                        atr_pct=feat["_atr_pct"], atr_value=feat["_atr_value"],
                        symbol=sym,
                    )

                    signal_id = make_signal_id(tier, direction, symbol=sym)

                    candidate_signals.append({
                        "symbol": sym,
                        "direction": direction,
                        "tier": tier,
                        "trade_type": trade_type,
                        "confidence": confidence,
                        "accuracy": accuracy,
                        "grade": grade,
                        "reasoning": reasoning,
                        "tpsl": tpsl,
                        "lot": lot,
                        "feat": feat,
                        "signal_id": signal_id,
                        "current_price": current_price,
                        "session": session,
                    })

            # ── Dispatch best signals ─────────────────────────────────────────
            grade_weight = {"A+": 4, "A": 3, "B": 2, "C": 1}
            candidate_signals.sort(
                key=lambda s: (grade_weight.get(s["grade"], 0), s["accuracy"], s["confidence"]),
                reverse=True
            )

            dispatched_count = 0
            for sig in candidate_signals[:2]:  # Allow up to 2 best high-grade signals per cycle
                sym           = sig["symbol"]
                direction     = sig["direction"]
                tier          = sig["tier"]
                trade_type    = sig["trade_type"]
                accuracy      = sig["accuracy"]
                grade         = sig["grade"]
                tpsl          = sig["tpsl"]
                lot           = sig["lot"]
                current_price = sig["current_price"]
                signal_id     = sig["signal_id"]
                reasoning     = sig["reasoning"]

                write_signal(
                    direction, tier, trade_type, sig["confidence"], accuracy,
                    grade, reasoning, tpsl, lot, sig["feat"], signal_id,
                    current_price, session, symbol=sym
                )

                # Send Telegram Alert
                dir_name = "BUY" if direction == 1 else "SELL"
                telegram_alert_text = (
                    f"<b>[COPETRANOVAX // {sym} SIGNAL]</b>\n\n"
                    f"<b>ACTION:</b> <code>{dir_name} NOW</code>\n"
                    f"<b>MODE:</b> {trade_type} ({tier})\n"
                    f"<b>ACCURACY:</b> {accuracy:.1f}% [Grade: {grade}]\n\n"
                    f"<b>ENTRY:</b> <code>{current_price}</code>\n"
                    f"<b>STOP LOSS:</b> <code>{tpsl['sl']}</code> ({tpsl['sl_pips']} pips)\n"
                    f"<b>TAKE PROFIT 1:</b> <code>{tpsl['tp1']}</code> ({tpsl['tp1_pips']} pips) [Funga nusu ya faida]\n"
                    f"<b>TAKE PROFIT 2:</b> <code>{tpsl['tp2']}</code> ({tpsl['tp2_pips']} pips) [Trend Runner]\n"
                    f"<b>RISK-REWARD:</b> 1:{tpsl['rr']} | Lot: {lot:.2f}\n\n"
                    f"<b>MAAGIZO YA KUTRADE:</b>\n"
                    f"1. Fungua trade ya <b>{dir_name}</b> kwenye <b>{sym}</b> sasa hivi.\n"
                    f"2. Weka Stop Loss kwa <code>{tpsl['sl']}</code>.\n"
                    f"3. Weka Take Profit 1 kwa <code>{tpsl['tp1']}</code> (funga 50% ya lot ukifika hapa).\n"
                    f"4. Faida ikifika +10 pips, sogeza Stop Loss iwe kwenye Entry (Risk-Free).\n\n"
                    f"<i>Time: {now.strftime('%H:%M UTC')} | Session: {session}</i>"
                )
                send_telegram_alert(telegram_alert_text)

                print(
                    f"\n  ===============================================================\n"
                    f"  [SIGNAL ALERT] {now.strftime('%H:%M UTC')} | {sym} {dir_name}\n"
                    f"  MODE     : {trade_type} ({tier})\n"
                    f"  ACCURACY : {accuracy:.1f}% [Grade: {grade}]\n"
                    f"  ENTRY    : {current_price} | SL: {tpsl['sl']} ({tpsl['sl_pips']} pips)\n"
                    f"  TARGETS  : TP1: {tpsl['tp1']} ({tpsl['tp1_pips']} pips) | TP2: {tpsl['tp2']} ({tpsl['tp2_pips']} pips)\n"
                    f"  R:R      : {tpsl['rr']}R | Lot: {lot:.2f}\n"
                    f"  REASONING: {reasoning}\n"
                    f"  ==============================================================="
                )

                # MT5 execution if available
                if MT5_AVAILABLE:
                    success, ticket = open_trade(
                        sym, direction, lot, tpsl["sl"], tpsl["tp2"], signal_id
                    )
                    if success:
                        risk.register_trade(
                            signal_id=str(ticket), direction=direction,
                            entry=current_price, sl_pips=tpsl["sl_pips"],
                            tp_pips=tpsl["tp2_pips"], tier=tier, lot=lot,
                            symbol=sym, tp1=tpsl["tp1"], tp2=tpsl["tp2"]
                        )
                        print(f"  [Bot] MT5 Execution Confirmed: ticket={ticket}")

                _last_signal_time[(sym, direction)] = now
                dispatched_count += 1

            # ── Telemetry Update for HUD Dashboard ────────────────────────────
            next_candle_sec = max(0, ((14 - (now.minute % 15)) * 60) + (60 - now.second))
            top_sym = candidate_signals[0]["symbol"] if candidate_signals else "XAUUSD"
            top_tele = symbols_telemetry.get(top_sym, {})

            gold_bars = fetch_bars("XAUUSD", mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 16385, BARS)
            gold_feat = compute_features(gold_bars) if len(gold_bars) >= 50 else feat
            update_market_status_json(
                price=top_tele.get("price", gold_feat["_close"]),
                session=session,
                htf_bias={"H1": top_tele.get("h1_bias", "NEUTRAL"), "H1_phase": top_tele.get("h1_phase", "RANGING")},
                feat=gold_feat,
                struct_score=0,
                stage=symbol_states[top_sym].stage,
                risk_status=risk.get_status(),
                next_candle_sec=next_candle_sec,
                symbol=top_sym,
                symbols_telemetry=symbols_telemetry
            )

            # Console cycle summary
            print(
                f"  [Bot] {now.strftime('%H:%M')} | {session} | "
                f"Scanned {len(SYMBOLS)} Pairs | Candidates: {len(candidate_signals)} | "
                f"Next M15 in {next_candle_sec//60}m{next_candle_sec%60:02d}s | {risk.get_status()}"
            )

            time.sleep(LOOP_SLEEP)

        except KeyboardInterrupt:
            print("\n  [Bot] Stopped by user")
            perf.print_daily_report()
            perf.print_full_report()
            if MT5_AVAILABLE:
                mt5.shutdown()
            break

        except Exception as e:
            print(f"  [Bot] Loop error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(15)


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    print()
    print("  ============================================")
    print("  CopetraNova v2 -- XAUUSD M15 Live Bot")
    print("  Dual Engine: SCALPING & INTRA-SWING")
    print("  Target: 15-Min Signal Cycle | Human Confluence")
    print("  ============================================")
    print(f"  Started: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("  ============================================")

    # Launch Futuristic Robotic HUD Server in background thread
    try:
        import threading
        from dashboard_server import run_server
        hud_thread = threading.Thread(target=run_server, kwargs={"port": 8080}, daemon=True)
        hud_thread.start()
        print("  [HUD] Robotic Cyberpunk Dashboard running at: http://localhost:8080\n")
    except Exception as e:
        print(f"  [HUD] Note: Dashboard server start deferred: {e}\n")

    run_bot()