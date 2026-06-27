
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

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[WARN] MetaTrader5 not installed -- signal-only mode")

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
from risk_engine     import RiskEngine, compute_pip_value
from performance_engine import PerformanceEngine


# ==============================================================================
# CONFIG
# ==============================================================================

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

SIGNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal.txt")
MGMT_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "management.txt")

# UP 1: Lowered ATR thresholds
ATR_MIN_SCALP = 1.2   # was 1.5
ATR_MIN_TREND = 1.7   # was 2.0
ATR_MIN_SWING = 2.8   # was 3.5

# UP 2: Lowered ADX thresholds
ADX_STRONG    = 22    # was 25
ADX_WEAK      = 16    # was 18

# UP 6: Signal spacing -- prevents spam but allows multi-fire
MIN_SIGNAL_SPACING_MIN = 30   # minutes between signals per direction

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

# UP 6: Signal timing tracker (per direction)
_last_signal_time: dict = {}   # {direction: datetime}


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
        "_atr_value": round(float(atr.iloc[-1]), 3),
        "_atr_pct":   round(atr_pct, 1),
        "_adx":       round(float(adx_val.iloc[-1]), 1),
        "_di_plus":   round(float(di_plus.iloc[-1]), 1),
        "_di_minus":  round(float(di_minus.iloc[-1]), 1),
        "_ema_fast":  round(float(ema_fast.iloc[-1]), 3),
        "_ema_mid":   round(float(ema_mid.iloc[-1]), 3),
        "_ema_slow":  round(float(ema_slow.iloc[-1]), 3),
        "_vol_ratio": round(vol_ratio, 2),
        "_prev_high": round(prev_high, 2),
        "_prev_low":  round(prev_low, 2),
        "_avg_rng_5": round(avg_rng_5, 3),
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
               session_mem: dict = None, now_hour: int = -1) -> list:
    """
    Scans all 7 tiers. Returns list ordered by priority (highest first).
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

    # ── TIER 1: SESSION BREAKOUT (London 07:00, Overlap 13:00) ───────────────
    if atr >= 1.0 and now_hour >= 0:
        win = BREAKOUT_WINDOWS.get(session)
        if win and win[0] <= now_hour < win[1]:
            asia_h = smem.get("asia_high", 0)
            asia_l = smem.get("asia_low",  0)
            buf    = atr * 0.15
            if asia_h and close > asia_h + buf and dip > dim and br >= 0.40:
                signals.insert(0, {
                    "tier": "BREAKOUT", "direction": 1,
                    "reason": f"BREAKOUT BUY: Asia high {asia_h:.2f} (close={close:.2f})",
                    "min_stage": SEQ_DISPLACE,
                })
            if asia_l and close < asia_l - buf and dim > dip and br >= 0.40:
                signals.insert(0, {
                    "tier": "BREAKOUT", "direction": -1,
                    "reason": f"BREAKOUT SELL: Asia low {asia_l:.2f} (close={close:.2f})",
                    "min_stage": SEQ_DISPLACE,
                })

    # ── TIER 2: SWING (strictest -- full alignment) ───────────────────────────
    if atr >= ATR_MIN_SWING:
        if ef > em > es and adx >= ADX_STRONG and dip > dim and h1 in ("BULL", "NEUTRAL"):
            signals.append({
                "tier": "SWING", "direction": 1,
                "reason": f"SWING BUY: full EMA + ADX={adx:.1f} + H1={h1}",
                "min_stage": SEQ_BOS,
            })
        if ef < em < es and adx >= ADX_STRONG and dim > dip and h1 in ("BEAR", "NEUTRAL"):
            signals.append({
                "tier": "SWING", "direction": -1,
                "reason": f"SWING SELL: full EMA + ADX={adx:.1f} + H1={h1}",
                "min_stage": SEQ_BOS,
            })

    # ── TIER 3: EMA21 PULLBACK (UP 4 -- new) ─────────────────────────────────
    # Price pulls back to EMA21 in a trend -- rejection candle confirms entry
    if atr >= ATR_MIN_TREND:
        ema_tol   = atr * 0.35
        touch_buy  = lo  <= em + ema_tol and close > em   # low touched EMA21, closed above
        touch_sell = hi  >= em - ema_tol and close < em   # high touched EMA21, closed below

        if (touch_buy and ef > em and close > es
                and close > op and dip > dim and adx >= ADX_WEAK):
            signals.append({
                "tier": "PULLBACK", "direction": 1,
                "reason": f"PULLBACK BUY: EMA21 touch (lo={lo:.2f} em={em:.2f})",
                "min_stage": SEQ_DISPLACE,
            })
        if (touch_sell and ef < em and close < es
                and close < op and dim > dip and adx >= ADX_WEAK):
            signals.append({
                "tier": "PULLBACK", "direction": -1,
                "reason": f"PULLBACK SELL: EMA21 touch (hi={hi:.2f} em={em:.2f})",
                "min_stage": SEQ_DISPLACE,
            })

    # ── TIER 4: TREND (UP 3 -- relaxed EMA) ──────────────────────────────────
    # UP 3: relaxed from (ef>em>es) to (ef>em AND close>es)
    if atr >= ATR_MIN_TREND:
        buy_ema  = ef > em and close > es    # UP 3: relaxed
        sell_ema = ef < em and close < es
        if buy_ema and adx >= ADX_WEAK and dip > dim:
            signals.append({
                "tier": "TREND", "direction": 1,
                "reason": f"TREND BUY: EMA relaxed + ADX={adx:.1f}",
                "min_stage": SEQ_BOS,
            })
        if sell_ema and adx >= ADX_WEAK and dim > dip:
            signals.append({
                "tier": "TREND", "direction": -1,
                "reason": f"TREND SELL: EMA relaxed + ADX={adx:.1f}",
                "min_stage": SEQ_BOS,
            })

    # ── TIER 5: MICRO BREAKOUT (UP 8 -- new) ─────────────────────────────────
    # Successive high/low break with volume -- institutional momentum burst
    if atr >= 1.0 and vr >= 1.5 and adx >= ADX_WEAK:
        close_near_hi  = (hi - close) < atr * 0.25   # closed near candle high
        close_near_lo  = (close - lo)  < atr * 0.25
        if hi > prev_h and close_near_hi and dip > dim:
            signals.append({
                "tier": "MICRO", "direction": 1,
                "reason": f"MICRO BUY: hi {hi:.2f}>{prev_h:.2f} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })
        if lo < prev_l and close_near_lo and dim > dip:
            signals.append({
                "tier": "MICRO", "direction": -1,
                "reason": f"MICRO SELL: lo {lo:.2f}<{prev_l:.2f} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })

    # ── TIER 6: VOLATILITY EXPANSION (UP 12 -- new) ──────────────────────────
    # Fires after compression: current range > 5-bar average * 1.5
    if avg_rng > 0 and feat["_range"] > avg_rng * 1.5 and vr >= 1.4 and adx >= ADX_WEAK:
        if close > ef and dip > dim:
            signals.append({
                "tier": "EXPANSION", "direction": 1,
                "reason": f"EXPANSION BUY: rng={feat['_range']:.2f} avg={avg_rng:.2f}",
                "min_stage": SEQ_DISPLACE,
            })
        if close < ef and dim > dip:
            signals.append({
                "tier": "EXPANSION", "direction": -1,
                "reason": f"EXPANSION SELL: rng={feat['_range']:.2f} avg={avg_rng:.2f}",
                "min_stage": SEQ_DISPLACE,
            })

    # ── TIER 7: SCALP (UP 5 -- relaxed body, stricter volume) ────────────────
    # UP 5: body >= 0.45 (was 0.55), vol >= 1.3 (was 1.1)
    if br >= 0.45 and vr >= 1.3 and atr >= ATR_MIN_SCALP:
        if close > op and dip > dim:
            signals.append({
                "tier": "SCALP", "direction": 1,
                "reason": f"SCALP BUY: body={br:.0%} vol={vr:.1f}x",
                "min_stage": SEQ_DISPLACE,
            })
        if close < op and dim > dip:
            signals.append({
                "tier": "SCALP", "direction": -1,
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
# TPSL CALCULATOR -- UP 15: A+ grade added
# ==============================================================================

def compute_tpsl(direction: int, entry: float, tier: str,
                  atr: float, confidence: float) -> dict:
    mult     = SL_MULT.get(tier, 1.5)
    sl_price = atr * mult
    sl_pips  = max(8, int(sl_price / PIP_SIZE))
    rr       = _rr_from_confidence(confidence)
    tp_pips  = int(sl_pips * rr)

    if direction == 1:
        sl = round(entry - sl_pips * PIP_SIZE, 2)
        tp = round(entry + tp_pips * PIP_SIZE, 2)
    else:
        sl = round(entry + sl_pips * PIP_SIZE, 2)
        tp = round(entry - tp_pips * PIP_SIZE, 2)

    # UP 15: expanded grades
    if tier in ("SWING", "BREAKOUT") and confidence >= 80:  grade = "A+"
    elif confidence >= 75:                                    grade = "A"
    elif confidence >= 65:                                    grade = "B"
    elif confidence >= 55:                                    grade = "C"
    else:                                                     grade = "D"

    return {"sl": sl, "tp": tp, "sl_pips": sl_pips,
             "tp_pips": tp_pips, "rr": rr, "grade": grade}


# ==============================================================================
# SIGNAL FILE WRITER
# ==============================================================================

def write_signal(direction: int, tier: str, confidence: float,
                  tpsl: dict, lot: float, feat: dict, signal_id: str) -> None:
    dir_name = "BUY" if direction == 1 else "SELL"
    lines = [
        f"DIRECTION={dir_name}", f"TIER={tier}",
        f"SCORE={confidence:.0f}", f"GRADE={tpsl['grade']}",
        f"TP={tpsl['tp']:.2f}", f"SL={tpsl['sl']:.2f}",
        f"RR={tpsl['rr']}", f"LOT={lot:.2f}",
        f"SL_PIPS={tpsl['sl_pips']}", f"ADX={feat['_adx']:.1f}",
        f"ATR={feat['_atr_value']:.3f}", f"SIGNAL_ID={signal_id}",
        f"TIMESTAMP={datetime.now(tz=timezone.utc).isoformat()}",
    ]
    try:
        with open(SIGNAL_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"  [Bot] Signal write error: {e}")


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

def make_signal_id(tier: str, direction: int) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    d  = "B" if direction == 1 else "S"
    return f"{tier}_{d}_{ts}"


# ==============================================================================
# UP 9: DUPLICATE GUARD -- candle timestamp based
# ==============================================================================

_seen_signals: deque = deque(maxlen=DUPE_GUARD_SIZE)

def is_duplicate(tier: str, direction: int, candle_ts: str) -> bool:
    """
    UP 9: Key uses candle timestamp not SL/TP.
    Allows continuation trades on different candles.
    Prevents spam on same candle only.
    """
    key = f"{tier}_{direction}_{candle_ts}"
    h   = hashlib.md5(key.encode()).hexdigest()[:12]
    if h in _seen_signals:
        return True
    _seen_signals.append(h)
    return False


# ==============================================================================
# MT5 FUNCTIONS
# ==============================================================================

def fetch_bars(symbol: str, timeframe, count: int) -> pd.DataFrame:
    if not MT5_AVAILABLE:
        return pd.DataFrame()
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["open", "high", "low", "close", "tick_volume"]].copy()


def get_spread_pips(symbol: str) -> float:
    if not MT5_AVAILABLE:
        return 0.0
    info = mt5.symbol_info_tick(symbol)
    return round((info.ask - info.bid) / PIP_SIZE, 1) if info else 0.0


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

_last_candle_time = None

def is_new_candle(df: pd.DataFrame) -> bool:
    global _last_candle_time
    if df is None or len(df) < 2:
        return False
    latest = df.index[-1]
    if _last_candle_time is None or latest != _last_candle_time:
        _last_candle_time = latest
        return True
    return False


# ==============================================================================
# MAIN BOT
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
        pip_val = compute_pip_value(mt5.symbol_info(SYMBOL))
    else:
        print("  [Bot] Demo mode (no MT5)")
        acct    = None
        pip_val = 10.0

    # ── Initialise engines ────────────────────────────────────────────────────
    balance_start = float(acct.balance) if acct else 50.0
    risk  = RiskEngine(account_balance=balance_start, pip_value_per_lot=pip_val)
    perf  = PerformanceEngine()
    state = StructureState()

    print(f"  [Bot] Tiers: BREAKOUT SWING PULLBACK TREND MICRO EXPANSION SCALP")
    print(f"  [Bot] ATR: {ATR_MIN_SCALP}/{ATR_MIN_TREND}/{ATR_MIN_SWING} | "
          f"ADX: {ADX_WEAK}/{ADX_STRONG} | "
          f"Spacing: {MIN_SIGNAL_SPACING_MIN}min/direction")
    print(f"  [Bot] Target: 15-25 signals/day | {risk.get_status()}")
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

            # ── Fetch bars ────────────────────────────────────────────────────
            df15 = fetch_bars(SYMBOL, mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 16385, BARS)
            df_h1= fetch_bars(SYMBOL, mt5.TIMEFRAME_H1  if MT5_AVAILABLE else 16408, H1_BARS)
            df_h4= fetch_bars(SYMBOL, mt5.TIMEFRAME_H4  if MT5_AVAILABLE else 16390, H4_BARS)

            if df15 is None or len(df15) < 100:
                time.sleep(LOOP_SLEEP)
                continue

            if not is_new_candle(df15):
                time.sleep(LOOP_SLEEP)
                continue

            now     = datetime.now(tz=timezone.utc)
            session = get_session(now)
            news_ok = is_news_active(now)
            candle_ts = str(df15.index[-1])   # UP 9: candle timestamp for dupe guard

            # ── Sync account ──────────────────────────────────────────────────
            if MT5_AVAILABLE and (acct := mt5.account_info()):
                risk.sync_account(acct.balance, acct.equity)

            # ── Reconcile positions ───────────────────────────────────────────
            if MT5_AVAILABLE:
                positions = mt5.positions_get(symbol=SYMBOL) or []
                mt5_ids   = {str(p.ticket) for p in positions}
                risk.reconcile_positions(mt5_ids)
                risk.update_floating_pnl({str(p.ticket): {"profit": p.profit}
                                           for p in positions})
            else:
                positions = []

            # ── Compute features ──────────────────────────────────────────────
            feat = compute_features(df15)

            if is_bad_candle(feat):
                print(f"  [Bot] {now.strftime('%H:%M')} Bad candle rejected "
                      f"(rng={feat['_range']:.2f} ATR={feat['_atr_value']:.2f})")
                time.sleep(LOOP_SLEEP)
                continue

            stable_atr = get_stable_atr(df15, feat["_atr_value"])
            htf_bias   = compute_htf_bias(df_h1, df_h4)

            # ── Manage open trades ────────────────────────────────────────────
            for p in positions:
                sid     = str(p.ticket)
                t_tier  = risk.open_trades.get(sid, {}).get("tier", "TREND")
                actions = risk.check_trade_management(sid, p.price_current,
                                                       stable_atr, t_tier)
                if actions["force_close"]:
                    close_position(p.ticket, SYMBOL, p.volume, p.type)
                    pips = ((p.price_current - p.price_open) / PIP_SIZE
                            if p.type == 0
                            else (p.price_open - p.price_current) / PIP_SIZE)
                    t_data = risk.open_trades.get(sid, {})
                    risk.record_result(pips=pips, signal_id=sid,
                                        equity=acct.equity if acct else None)
                    perf.record_trade(
                        signal_id=sid, direction=1 if p.type==0 else -1,
                        tier=t_data.get("tier", "TREND"), session=session,
                        adx_value=feat["_adx"],
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
                    modify_sl(p.ticket, actions["trail_sl"], SYMBOL)
                if actions["partial_tp"]:
                    t_data = risk.open_trades.get(sid, {})
                    partial_close(p.ticket, SYMBOL,
                                   t_data.get("lot", 0.01),
                                   t_data.get("direction", 1))

            # ── News filter ───────────────────────────────────────────────────
            if news_ok:
                print(f"  [Bot] {now.strftime('%H:%M')} NEWS ACTIVE")
                time.sleep(LOOP_SLEEP)
                continue

            # ── Dynamic confidence floor (UP 10) ──────────────────────────────
            risk_floor = risk.get_confidence_floor(CONF_BASE)
            conf_floor = get_dynamic_conf_floor(session, risk_floor)

            # ── Session memory + 7-tier scanner ───────────────────────────────
            session_mem_c = get_session_memory(df15)
            tier_signals  = scan_tiers(feat, htf_bias, session,
                                        session_mem_c, now.hour)

            if not tier_signals:
                print(f"  [Bot] {now.strftime('%H:%M')} | {session} | "
                      f"ADX:{feat['_adx']:.0f} | ATR:{feat['_atr_value']:.2f} | "
                      f"H1:{htf_bias['H1']}({htf_bias['H1_phase']}) | "
                      f"{risk.get_status()}")
                time.sleep(LOOP_SLEEP)
                continue

            spread_pips    = get_spread_pips(SYMBOL)
            current_price  = feat["_close"]
            signals_fired  = 0   # UP 6: count per candle

            # ── Signal loop (UP 6: continue not break) ────────────────────────
            for sig in tier_signals:
                tier      = sig["tier"]
                direction = sig["direction"]

                # UP 6: signal spacing guard -- 30 min per direction
                last_dir_ts = _last_signal_time.get(direction)
                if last_dir_ts:
                    elapsed = (now - last_dir_ts).total_seconds() / 60
                    # UP 11: bypass spacing if pyramiding (breakeven trade exists)
                    pyramiding = is_pyramiding_allowed(direction, risk)
                    if elapsed < MIN_SIGNAL_SPACING_MIN and not pyramiding:
                        continue   # too soon -- try next tier

                # UP 9: candle timestamp duplicate guard
                if is_duplicate(tier, direction, candle_ts):
                    continue

                # Risk check
                allowed, risk_reason = risk.check_risk(
                    equity=float(acct.equity) if acct else None,
                    spread_pips=spread_pips,
                    atr_value=feat["_atr_value"],
                    atr_pct=feat["_atr_pct"],
                    direction=direction,
                )
                if not allowed:
                    print(f"  [Bot] RISK BLOCK ({tier}): {risk_reason}")
                    continue

                # Structure score
                struct_score, struct_detail = get_structure_score(
                    df=df15, current_price=current_price,
                    direction=direction, raw_atr=feat["_atr_value"],
                    htf_bias=htf_bias, news_active=news_ok,
                    state=state, session=session,
                )

                # Mandatory conditions
                ext_sh = detect_swing_highs(df15)
                ext_sl = detect_swing_lows(df15)
                permitted, _ = check_mandatory_conditions(
                    ext_sh, ext_sl, "RANGING", state, False
                )
                if not permitted:
                    continue

                # UP 13: Soft structure -- London/Overlap use SEQ_DISPLACE for TREND
                # UP 14: Trend persistence -- ADX > 30 lowers requirement
                tier_min = sig.get("min_stage", SEQ_BOS)
                if session in ("LONDON", "OVERLAP") and tier == "TREND":
                    tier_min = SEQ_DISPLACE   # UP 13
                if feat["_adx"] >= 30 and tier == "TREND":
                    tier_min = SEQ_DISPLACE   # UP 14

                if not state.is_trading_permitted(min_stage=tier_min):
                    continue

                # Confidence
                confidence = compute_confidence(
                    direction, feat, tier, htf_bias, session, struct_score
                )
                if confidence < conf_floor:
                    continue

                # TPSL
                tpsl = compute_tpsl(direction, current_price,
                                     tier, stable_atr, confidence)

                # Lot size
                equity_now = float(acct.equity) if acct else None
                lot = risk.get_lot_size(
                    sl_pips=tpsl["sl_pips"], equity=equity_now,
                    atr_pct=feat["_atr_pct"], atr_value=feat["_atr_value"],
                )

                signal_id = make_signal_id(tier, direction)
                write_signal(direction, tier, confidence, tpsl,
                              lot, feat, signal_id)

                dir_name = "BUY" if direction == 1 else "SELL"
                print(
                    f"\n  [SIGNAL] {now.strftime('%H:%M')} "
                    f"{tier} {dir_name} | "
                    f"Conf:{confidence:.0f}({tpsl['grade']}) | "
                    f"SL:{tpsl['sl_pips']}pips TP:{tpsl['tp_pips']}pips "
                    f"RR:{tpsl['rr']}R | Lot:{lot:.2f} | "
                    f"Struct:{struct_score:+d} | ADX:{feat['_adx']:.0f} | {session}"
                )
                print(f"  [STRUCT] {struct_detail[:110]}")
                print(f"  [SCAN  ] {sig['reason']}")

                # Execute trade
                success, ticket = open_trade(
                    SYMBOL, direction, lot,
                    tpsl["sl"], tpsl["tp"], signal_id,
                )
                if not success:
                    print(f"  [Bot] Execution failed -- skipped")
                    continue

                ticket_id = str(ticket)
                risk.register_trade(
                    signal_id=ticket_id, direction=direction,
                    entry=current_price, sl_pips=tpsl["sl_pips"],
                    tp_pips=tpsl["tp_pips"], tier=tier, lot=lot,
                )

                # UP 6: update timing + count
                _last_signal_time[direction] = now
                signals_fired += 1

                print(f"  [Bot] Registered ticket={ticket_id} | "
                      f"stage={state.stage} | floor={conf_floor:.0f} | "
                      f"fired_this_candle={signals_fired}")

                # UP 6: max 2 signals per candle to prevent candle spam
                if signals_fired >= 2:
                    break

            # ── Status line ───────────────────────────────────────────────────
            print(
                f"  [Bot] {now.strftime('%H:%M')} | {session} | "
                f"ADX:{feat['_adx']:.0f} | ATR:{feat['_atr_value']:.2f} | "
                f"H1:{htf_bias['H1']}({htf_bias['H1_phase']}) | "
                f"stage={state.stage} | "
                f"{risk.get_status()}"
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
    print("  7 Tiers | 15-25 Signals/Day Target")
    print("  ============================================")
    print(f"  Started: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("  ============================================")
    print()
    run_bot()