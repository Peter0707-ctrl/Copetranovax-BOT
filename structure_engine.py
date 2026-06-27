"""
CopetraNova -- structure_engine.py  (v10)
==========================================
Institutional-grade sequential structure engine for XAUUSD M15.

ALL 7 REMAINING REFINEMENTS FIXED:

  FIX 1 : BOS continuation check strengthened.
           body > ATR * 0.25 AND body/range > 0.55 required.
           Tiny doji candles after breakout no longer qualify.

  FIX 2 : State machine RETEST_REACTED requires price to HOLD.
           Price must remain above FVG mid (BUY) or below (SELL).
           Failure to hold resets state -- dead-cat bounces filtered.

  FIX 3 : Orderblock impulse quality filter.
           Impulse body ratio >= 0.70 required for OB creation.
           Weak displacement origins no longer create institutional OBs.

  FIX 4 : Liquidity pool hierarchical merging.
           Pools within POOL_MERGE_PIPS of each other are merged.
           Prevents duplicate scoring of same liquidity cluster.

  FIX 5 : DisplacementLeg class -- impulse lifecycle memory.
           Tracks: origin, extreme, efficiency, retracement, FVG count.
           continuation_probability() scores ongoing impulse quality.
           Stored in StructureState for persistence across candles.

  FIX 6 : Volatility-of-volatility added to regime detection.
           detect_vov() classifies expansion as STABLE or CHAOTIC.
           CHAOTIC expansion = reduced structure weights.
           Handles NY / news-drift behavior differences.

  FIX 7 : Volume / orderflow proxy engine.
           compute_orderflow_proxy() uses tick_volume + candle efficiency
           + imbalance persistence as participation approximation.
           High-participation signals score higher in continuation bucket.

ALL v9 FIXES RETAINED:
  - Fractal swings (internal lookback=3, external lookback=12)
  - Body-break BOS on external swings
  - CHOCH on internal swings with prior structure validation
  - Failed CHOCH requires breakout-then-reclaim
  - Rejection candle at retest zone (SEQ_RETEST_REACTED stage)
  - Validated OB (BOS + FVG + body integrity)
  - Pool age decay with session origin
  - OTE only after displacement
  - Diminishing returns within evidence clusters
  - Execution quality removed from structure engine
  - Sequential state machine with is_trading_permitted()
  - Mandatory conditions gate
  - Directional efficiency regime
  - Volatility compression detection
  - HTF structural phases
  - Time decay on proximity scores
  - Premium / Discount array
  - Score clamped to [-10, +10]
  - All non-ASCII removed

Public API:
  StructureState()
  get_structure_score(df, price, direction, raw_atr,
                      htf_bias, news_active, state, session)
  check_mandatory_conditions(...)
  scalp_break_buy_level(df, atr)
  scalp_break_sell_level(df, atr)
"""

import math
import pandas as pd


# ==============================================================================
# CONFIG
# ==============================================================================

SWING_LOOKBACK_INT    = 3
SWING_LOOKBACK_EXT    = 12
PROXIMITY_FACTOR      = 1.5
EQUAL_LEVEL_TOL       = 0.5
EQUAL_CANDLE_GAP      = 5
POOL_MERGE_PIPS       = 2.0    # FIX 4: merge pools within this many pips
ROUND_STEP            = 10
MICRO_BREAK_BUFF      = 0.05
MAX_STRUCT_SCORE      = 10
SWEEP_WICK_RATIO      = 0.40
PIP_SIZE              = 0.10
ATR_SMOOTH_WINDOW     = 20
ATR_SPIKE_FACTOR      = 1.5
TIME_DECAY_FACTOR     = 20
DISP_ATR_MULT         = 1.2
DISP_BODY_RATIO       = 0.65
INVALIDATION_BUFF     = 0.5
FVG_LOOKBACK          = 50
STATE_MAX_AGE         = 50
MAX_POOL_AGE          = 96
MIN_OB_BODY_MULT      = 0.3
OB_IMPULSE_BODY_RATIO = 0.70   # FIX 3: minimum impulse body ratio for OB
BOS_CONT_ATR_MULT     = 0.25   # FIX 1: continuation body > ATR * this
BOS_CONT_BODY_RATIO   = 0.55   # FIX 1: continuation body/range > this
OTE_LOW_FIBO          = 0.618
OTE_HIGH_FIBO         = 0.786

# Category caps
REVERSAL_CAP     = 6
CONTINUATION_CAP = 5
LIQUIDITY_CAP    = 4
CONTEXT_CAP      = 5

# Sequential states
SEQ_IDLE           = "IDLE"
SEQ_SWEEP          = "SWEEP_DETECTED"
SEQ_DISPLACE       = "DISPLACEMENT"
SEQ_BOS            = "BOS_CONFIRMED"
SEQ_RETEST         = "RETEST_PENDING"
SEQ_RETEST_REACTED = "RETEST_REACTED"
SEQ_CONFIRMED      = "FULLY_CONFIRMED"

SEQ_ORDER = [SEQ_IDLE, SEQ_SWEEP, SEQ_DISPLACE,
             SEQ_BOS, SEQ_RETEST, SEQ_RETEST_REACTED, SEQ_CONFIRMED]

# Regime weights
REGIME_WEIGHTS = {
    "TRENDING":    {"choch": 0.5, "sweep": 0.7, "bos": 1.5, "session": 1.1},
    "RANGING":     {"choch": 1.0, "sweep": 1.5, "bos": 0.5, "session": 0.9},
    "EXPANDING":   {"choch": 0.6, "sweep": 1.0, "bos": 1.3, "session": 1.0},
    "CHOPPY":      {"choch": 0.4, "sweep": 0.8, "bos": 0.4, "session": 0.7},
    "NEWS_SPIKE":  {"choch": 0.1, "sweep": 0.1, "bos": 0.1, "session": 0.1},
    "LOW_VOL":     {"choch": 0.8, "sweep": 1.0, "bos": 0.6, "session": 0.6},
    "COMPRESSION": {"choch": 0.9, "sweep": 1.2, "bos": 0.8, "session": 0.8},
    "CHAOTIC":     {"choch": 0.3, "sweep": 0.6, "bos": 0.5, "session": 0.5},
}

# Session personality
SESSION_BEHAVIOR = {
    "ASIA":    {"style": "accumulation", "choch_mult": 0.8, "ob_mult": 1.2,
                "bos_mult": 0.7, "sweep_mult": 1.1},
    "LONDON":  {"style": "expansion",    "choch_mult": 0.6, "ob_mult": 0.9,
                "bos_mult": 1.5, "sweep_mult": 0.9},
    "OVERLAP": {"style": "continuation", "choch_mult": 0.7, "ob_mult": 1.0,
                "bos_mult": 1.3, "sweep_mult": 1.0},
    "NY":      {"style": "reversal",     "choch_mult": 1.2, "ob_mult": 1.1,
                "bos_mult": 0.9, "sweep_mult": 1.2},
}


# ==============================================================================
# DIRECTION NORMALISER
# ==============================================================================

def _norm(direction):
    return 1 if direction == 1 else -1


# ==============================================================================
# ATR HELPERS
# ==============================================================================

def _compute_atr_series(df, period=14):
    prev = df["close"].shift(1)
    tr   = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def get_stable_atr(df, raw_atr, period=14):
    if len(df) < ATR_SMOOTH_WINDOW + period:
        return raw_atr
    atr_s = _compute_atr_series(df, period)
    slow  = float(atr_s.rolling(ATR_SMOOTH_WINDOW).mean().iloc[-1])
    return min(raw_atr, slow * ATR_SPIKE_FACTOR) if slow > 0 else raw_atr


# ==============================================================================
# FRACTAL SWING DETECTION
# ==============================================================================

def _detect_swings_raw(df, lookback, side="high"):
    col    = df["high"] if side == "high" else df["low"]
    n      = len(col)
    swings = []
    for i in range(lookback, n - lookback):
        window  = col.iloc[i - lookback: i + lookback + 1]
        current = round(float(col.iloc[i]), 2)
        extreme = round(float(window.max() if side == "high" else window.min()), 2)
        if current != extreme:
            continue
        if swings and abs(swings[-1][1] - current) < PIP_SIZE * 0.5:
            continue
        swings.append((df.index[i], current))
    return list(reversed(swings))


def detect_swing_highs(df, lookback=SWING_LOOKBACK_EXT):
    return _detect_swings_raw(df, lookback, "high")


def detect_swing_lows(df, lookback=SWING_LOOKBACK_EXT):
    return _detect_swings_raw(df, lookback, "low")


def detect_internal_highs(df):
    return _detect_swings_raw(df, SWING_LOOKBACK_INT, "high")


def detect_internal_lows(df):
    return _detect_swings_raw(df, SWING_LOOKBACK_INT, "low")


# ==============================================================================
# TIME DECAY
# ==============================================================================

def _swing_age_candles(swing_ts, df):
    try:
        last = pd.to_datetime(df.index[-1])
        sts  = pd.to_datetime(swing_ts)
        return abs((last - sts).total_seconds() / 60.0 / 15.0)
    except Exception:
        return 999.0


def _time_decay(value, age_candles):
    if age_candles >= 999:
        return value * 0.2
    return value * math.exp(-age_candles / TIME_DECAY_FACTOR)


# ==============================================================================
# DIMINISHING RETURNS
# ==============================================================================

def _diminishing_sum(scores):
    """
    Correlated evidence from same move should not fully stack.
    First=100%, Second=50%, Third=33%, Fourth=25%.
    """
    if not scores:
        return 0.0
    total = 0.0
    for idx, s in enumerate(sorted(scores, reverse=True)):
        total += s / (idx + 1.0)
    return total


# ==============================================================================
# PREMIUM / DISCOUNT
# ==============================================================================

def get_premium_discount(ext_sh, ext_sl, current_price):
    if not ext_sh or not ext_sl:
        return {"zone": "UNKNOWN", "eq": 0, "pct": 0.5}
    rh  = ext_sh[0][1]
    rl  = ext_sl[0][1]
    if rh <= rl:
        return {"zone": "UNKNOWN", "eq": 0, "pct": 0.5}
    pct  = (current_price - rl) / (rh - rl)
    zone = "DISCOUNT" if pct < 0.45 else "PREMIUM" if pct > 0.55 else "EQUILIBRIUM"
    return {"zone": zone, "eq": (rh + rl) / 2.0, "pct": round(pct, 3)}


# ==============================================================================
# OTE ZONE -- ONLY AFTER DISPLACEMENT
# ==============================================================================

def detect_ote_zone(ext_sh, ext_sl, current_price, direction,
                     displacement_occurred=False):
    if not displacement_occurred:
        return False, "", 0.0
    if not ext_sh or not ext_sl:
        return False, "", 0.0
    sh  = ext_sh[0][1]
    sl  = ext_sl[0][1]
    rng = sh - sl
    if rng <= 0:
        return False, "", 0.0
    if direction == 1:
        ote_low  = sl + (1 - OTE_HIGH_FIBO) * rng
        ote_high = sl + (1 - OTE_LOW_FIBO)  * rng
        if ote_low <= current_price <= ote_high:
            return True, f"BUY OTE [{ote_low:.2f}-{ote_high:.2f}]", 3.0
    else:
        ote_low  = sh - (1 - OTE_LOW_FIBO)  * rng
        ote_high = sh - (1 - OTE_HIGH_FIBO) * rng
        if ote_low <= current_price <= ote_high:
            return True, f"SELL OTE [{ote_low:.2f}-{ote_high:.2f}]", 3.0
    return False, "", 0.0


# ==============================================================================
# FIX 4: LIQUIDITY POOL WITH AGE DECAY AND HIERARCHICAL MERGE
# ==============================================================================

class LiquidityPool:
    """Pool with age decay and session origin tracking."""
    def __init__(self, price, touches, side, session="UNKNOWN"):
        self.price          = price
        self.touches        = touches
        self.side           = side
        self.swept          = False
        self.active         = True
        self.sweep_ts       = None
        self.age_candles    = 0
        self.session_origin = session

    def tick(self):
        self.age_candles += 1
        if self.age_candles > MAX_POOL_AGE:
            self.active = False

    def strength(self):
        base  = min(self.touches, 4)
        decay = max(0.2, 1.0 - (self.age_candles / MAX_POOL_AGE))
        return round(base * decay, 2)

    def update(self, df):
        self.tick()
        if not self.active:
            return
        last_high = float(df["high"].iloc[-1])
        last_low  = float(df["low"].iloc[-1])
        if self.side == "high" and last_high > self.price:
            self.swept = True
            self.active = False
            self.sweep_ts = df.index[-1]
        elif self.side == "low" and last_low < self.price:
            self.swept = True
            self.active = False
            self.sweep_ts = df.index[-1]


def find_equal_levels(swings, tol_pips=EQUAL_LEVEL_TOL,
                       min_candle_gap=EQUAL_CANDLE_GAP):
    tol   = tol_pips * PIP_SIZE
    pools = []
    used  = set()
    for i, (t_i, p_i) in enumerate(swings):
        if i in used:
            continue
        cluster = [p_i]
        for j, (t_j, p_j) in enumerate(swings):
            if j <= i or j in used:
                continue
            if abs(p_i - p_j) > tol:
                continue
            gap = abs((t_i - t_j).total_seconds() / 60.0 / 15.0)
            if gap < min_candle_gap:
                continue
            cluster.append(p_j)
            used.add(j)
        if len(cluster) >= 2:
            pools.append((round(sum(cluster) / len(cluster), 2), len(cluster)))
        used.add(i)
    return sorted(pools, key=lambda x: x[1], reverse=True)


def _merge_nearby_pools(pool_list, merge_pips=POOL_MERGE_PIPS):
    """
    FIX 4: Merge pools within merge_pips of each other.
    Prevents duplicate scoring of fragmented same liquidity cluster.
    Example: 3360.1, 3360.4, 3360.8 all become one pool.
    """
    if len(pool_list) <= 1:
        return pool_list
    merge_dist = merge_pips * PIP_SIZE * 10   # convert pips to price
    merged     = []
    used       = set()
    for i, (p_i, c_i) in enumerate(pool_list):
        if i in used:
            continue
        cluster_p = [p_i]
        cluster_c = [c_i]
        for j, (p_j, c_j) in enumerate(pool_list):
            if j <= i or j in used:
                continue
            if abs(p_i - p_j) <= merge_dist:
                cluster_p.append(p_j)
                cluster_c.append(c_j)
                used.add(j)
        avg_price   = round(sum(cluster_p) / len(cluster_p), 2)
        total_count = sum(cluster_c)
        merged.append((avg_price, total_count))
        used.add(i)
    return sorted(merged, key=lambda x: x[1], reverse=True)


def build_liquidity_pools(ext_sh, ext_sl, session="UNKNOWN"):
    """FIX 4: Build pools with merge step."""
    pools = []
    raw_h = find_equal_levels(ext_sh)
    raw_l = find_equal_levels(ext_sl)
    merged_h = _merge_nearby_pools(raw_h)
    merged_l = _merge_nearby_pools(raw_l)
    for price, count in merged_h:
        pools.append(LiquidityPool(price, count, "high", session))
    for price, count in merged_l:
        pools.append(LiquidityPool(price, count, "low", session))
    return pools


# ==============================================================================
# FVG ENGINE
# ==============================================================================

def detect_fvg(df, direction, lookback=FVG_LOOKBACK):
    start = max(2, len(df) - lookback)
    fvgs  = []
    for i in range(start, len(df)):
        c0h = float(df["high"].iloc[i - 2])
        c0l = float(df["low"].iloc[i - 2])
        c2h = float(df["high"].iloc[i])
        c2l = float(df["low"].iloc[i])
        if direction == 1 and c0h < c2l:
            fvgs.append({"top": c2l, "bottom": c0h,
                          "mid": (c2l + c0h) / 2.0,
                          "time": df.index[i], "filled": False})
        elif direction == -1 and c0l > c2h:
            fvgs.append({"top": c0l, "bottom": c2h,
                          "mid": (c0l + c2h) / 2.0,
                          "time": df.index[i], "filled": False})
    for fvg in fvgs:
        later = df[pd.to_datetime(df.index) > pd.to_datetime(fvg["time"])]
        if len(later) == 0:
            continue
        if direction == 1 and float(later["low"].min()) <= fvg["mid"]:
            fvg["filled"] = True
        elif direction == -1 and float(later["high"].max()) >= fvg["mid"]:
            fvg["filled"] = True
    return list(reversed(fvgs))


def nearest_unmitigated_fvg(fvgs, current_price, stable_atr):
    for fvg in fvgs:
        if fvg["filled"]:
            continue
        if abs(current_price - fvg["mid"]) <= stable_atr * 2.0:
            return fvg
    return None


# ==============================================================================
# FIX 6: REGIME -- VOLATILITY OF VOLATILITY
# ==============================================================================

def directional_efficiency(series, lookback=20):
    if len(series) < lookback:
        return 0.5
    sub   = series.iloc[-lookback:]
    net   = abs(float(sub.iloc[-1]) - float(sub.iloc[0]))
    total = float(sub.diff().abs().dropna().sum())
    return min(1.0, net / total) if total > 0 else 0.5


def detect_volatility_regime(df, stable_atr):
    """FIX 6: Detect volatility compression or expansion cycle."""
    if len(df) < 30 or stable_atr <= 0:
        return "NORMAL"
    atr_s      = _compute_atr_series(df, 14)
    recent_atr = float(atr_s.iloc[-5:].mean())
    longer_atr = float(atr_s.iloc[-20:].mean())
    if longer_atr <= 0:
        return "NORMAL"
    ratio = recent_atr / longer_atr
    if ratio < 0.65:
        return "COMPRESSION"
    elif ratio > 1.45:
        return "EXPANSION"
    return "NORMAL"


def detect_vov(df):
    """
    FIX 6: Volatility-of-volatility.
    Measures how erratically ATR itself is changing.
    CHAOTIC = ATR changing unpredictably = reduce structure weights.
    STABLE  = ATR changing smoothly = normal structure weights.
    """
    if len(df) < 25:
        return "STABLE"
    atr_s      = _compute_atr_series(df, 14)
    atr_chg    = atr_s.diff().abs().iloc[-10:]
    atr_mean   = float(atr_s.iloc[-10:].mean())
    if atr_mean <= 0:
        return "STABLE"
    chg_std    = float(atr_chg.std())
    vov_ratio  = chg_std / atr_mean
    return "CHAOTIC" if vov_ratio > 0.30 else "STABLE"


def detect_market_regime(df, stable_atr):
    """
    FIX 6: Regime combines directional efficiency + ATR pct
    + volatility compression + volatility-of-volatility.
    """
    if len(df) < 50:
        return "RANGING"

    close   = df["close"]
    atr_s   = _compute_atr_series(df, 14)
    atr_pct = float(atr_s.rolling(min(100, len(df))).rank(pct=True).iloc[-1] * 100)
    de      = directional_efficiency(close, 20)
    ema20   = close.ewm(span=20, adjust=False).mean()
    slope   = 0.0
    if stable_atr > 0 and len(ema20) >= 5:
        slope = abs(float(ema20.iloc[-1]) - float(ema20.iloc[-5])) / (5.0 * stable_atr)

    vol_regime = detect_volatility_regime(df, stable_atr)
    vov        = detect_vov(df)

    # CHAOTIC expansion overrides normal expansion classification
    if atr_pct > 60 and vov == "CHAOTIC":
        return "CHAOTIC"
    if vol_regime == "COMPRESSION":
        return "COMPRESSION"
    if atr_pct > 94:
        return "NEWS_SPIKE"
    if atr_pct < 12:
        return "LOW_VOL"
    if de > 0.65 and atr_pct > 40:
        return "TRENDING"
    if de > 0.55 and slope > 0.20:
        return "TRENDING"
    if de < 0.35 and atr_pct < 50:
        return "RANGING"
    if de < 0.25:
        return "CHOPPY"
    return "EXPANDING"


# ==============================================================================
# FIX 7: ORDERFLOW / VOLUME PROXY
# ==============================================================================

def compute_orderflow_proxy(df, stable_atr):
    """
    FIX 7: Approximate institutional orderflow participation.
    MT5 spot XAUUSD lacks centralised volume -- approximated via:

    A. Tick volume acceleration: current vol vs 10-bar average.
    B. Candle spread efficiency: body/range on recent candles.
    C. Imbalance persistence: consecutive same-direction closes.

    Returns dict with score (0-8) and detail string.
    Higher score = stronger participation = higher conviction.
    """
    result = {"score": 0, "detail": "no volume data", "vol_ratio": 1.0}

    if len(df) < 15:
        return result

    details = []
    score   = 0

    # A. Tick volume acceleration
    if "tick_volume" in df.columns:
        vol     = df["tick_volume"]
        avg_vol = float(vol.rolling(10).mean().iloc[-1])
        cur_vol = float(vol.iloc[-1])
        if avg_vol > 0:
            vol_ratio = cur_vol / avg_vol
            result["vol_ratio"] = round(vol_ratio, 2)
            if vol_ratio > 2.0:
                score += 3; details.append(f"vol_accel={vol_ratio:.1f}x(strong)")
            elif vol_ratio > 1.5:
                score += 2; details.append(f"vol_accel={vol_ratio:.1f}x")
            elif vol_ratio > 1.2:
                score += 1; details.append(f"vol_accel={vol_ratio:.1f}x(mild)")
            else:
                details.append(f"vol_accel={vol_ratio:.1f}x(weak)")
    else:
        details.append("tick_vol=unavailable")

    # B. Candle spread efficiency (body dominance)
    last_rng  = float(df["high"].iloc[-1] - df["low"].iloc[-1])
    last_body = abs(float(df["close"].iloc[-1]) - float(df["open"].iloc[-1]))
    if last_rng > 0:
        eff = last_body / last_rng
        if eff > 0.75:
            score += 3; details.append(f"efficiency={eff:.0%}(strong)")
        elif eff > 0.55:
            score += 2; details.append(f"efficiency={eff:.0%}")
        elif eff > 0.40:
            score += 1; details.append(f"efficiency={eff:.0%}(mild)")
        else:
            details.append(f"efficiency={eff:.0%}(weak)")

    # C. Imbalance persistence (last 3 candles same direction)
    closes = df["close"].iloc[-4:]
    opens  = df["open"].iloc[-4:]
    dirs   = [1 if closes.iloc[i] > opens.iloc[i] else -1
              for i in range(len(closes))]
    if len(dirs) >= 3 and dirs[-1] == dirs[-2] == dirs[-3]:
        score += 2; details.append("persistence=3 bars")
    elif len(dirs) >= 2 and dirs[-1] == dirs[-2]:
        score += 1; details.append("persistence=2 bars")

    result["score"]  = score
    result["detail"] = " | ".join(details)
    return result


# ==============================================================================
# HTF STRUCTURAL PHASE
# ==============================================================================

def evaluate_htf_phase(htf_bias, direction):
    if not htf_bias:
        return 0.0, 0.0, "no HTF data"
    bonus   = 0.0
    penalty = 0.0
    details = []
    h1_dir   = htf_bias.get("H1",       "NEUTRAL")
    h1_phase = htf_bias.get("H1_phase", "RANGING")
    h4_dir   = htf_bias.get("H4",       "NEUTRAL")
    h4_phase = htf_bias.get("H4_phase", "RANGING")

    if direction == 1:
        if h1_dir == "BEAR" and h1_phase == "IMPULSE":
            penalty += 5.0; details.append("BLOCKED:H1 bear impulse")
        elif h1_dir == "BULL" and h1_phase == "PULLBACK":
            bonus += 5.0; details.append("H1 bull pullback(optimal BUY)")
        elif h1_dir == "BULL" and h1_phase == "IMPULSE":
            bonus += 3.0; details.append("H1 bull impulse")
        elif h1_dir == "BULL":
            bonus += 2.0; details.append("H1 bullish")
        elif h1_dir == "BEAR":
            penalty += 2.0; details.append("H1 bearish(adverse)")
        if h4_dir == "BEAR" and h4_phase == "IMPULSE":
            penalty += 3.0; details.append("H4 bear impulse(major)")
        elif h4_dir == "BULL":
            bonus += 2.0; details.append("H4 bullish")
    else:
        if h1_dir == "BULL" and h1_phase == "IMPULSE":
            penalty += 5.0; details.append("BLOCKED:H1 bull impulse")
        elif h1_dir == "BEAR" and h1_phase == "PULLBACK":
            bonus += 5.0; details.append("H1 bear pullback(optimal SELL)")
        elif h1_dir == "BEAR" and h1_phase == "IMPULSE":
            bonus += 3.0; details.append("H1 bear impulse")
        elif h1_dir == "BEAR":
            bonus += 2.0; details.append("H1 bearish")
        elif h1_dir == "BULL":
            penalty += 2.0; details.append("H1 bullish(adverse)")
        if h4_dir == "BULL" and h4_phase == "IMPULSE":
            penalty += 3.0; details.append("H4 bull impulse(major)")
        elif h4_dir == "BEAR":
            bonus += 2.0; details.append("H4 bearish")

    return bonus, penalty, " | ".join(details) if details else "HTF neutral"


# ==============================================================================
# FIX 1: BOS -- BODY BREAK + STRONG CONTINUATION
# ==============================================================================

def detect_bos(ext_sh, ext_sl, df, direction, stable_atr):
    """
    FIX 1: BOS requires:
    1. BODY fully clears the level (min(open,close) > level + buf)
    2. Strong continuation candle: body > ATR*0.25 AND body/range > 0.55

    Tiny doji candles after breakout no longer qualify as strong BOS.
    Returns (basic_ok: bool, strong_ok: bool)
    """
    buf        = stable_atr * MICRO_BREAK_BUFF
    last_close = float(df["close"].iloc[-1])
    last_open  = float(df["open"].iloc[-1])
    last_high  = float(df["high"].iloc[-1])
    last_low   = float(df["low"].iloc[-1])
    body_top   = max(last_open, last_close)
    body_bot   = min(last_open, last_close)
    body       = abs(last_close - last_open)
    rng        = last_high - last_low

    basic_ok = False
    if direction == 1 and len(ext_sh) >= 2:
        basic_ok = body_bot > ext_sh[1][1] + buf
    elif direction == -1 and len(ext_sl) >= 2:
        basic_ok = body_top < ext_sl[1][1] - buf

    if not basic_ok:
        return False, False

    # FIX 1: strong continuation requires dominant body
    body_ratio  = (body / rng) if rng > 0 else 0
    strong_body = (body > stable_atr * BOS_CONT_ATR_MULT
                   and body_ratio > BOS_CONT_BODY_RATIO)

    # Also check directional alignment
    directional = (last_close > last_open) if direction == 1 else (last_close < last_open)

    strong_ok = basic_ok and strong_body and directional
    return True, strong_ok


# ==============================================================================
# REJECTION CANDLE DETECTION
# ==============================================================================

def _detect_rejection_candle(df, direction):
    if len(df) < 1:
        return False
    last_high  = float(df["high"].iloc[-1])
    last_low   = float(df["low"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    last_open  = float(df["open"].iloc[-1])
    rng        = last_high - last_low
    body       = abs(last_close - last_open)
    if rng <= 0:
        return False
    if direction == 1:
        lw = min(last_open, last_close) - last_low
        return lw > body * 0.5 and last_close > last_open
    else:
        uw = last_high - max(last_open, last_close)
        return uw > body * 0.5 and last_close < last_open


# ==============================================================================
# CHOCH ON INTERNAL STRUCTURE
# ==============================================================================

def detect_choch(int_sh, int_sl, df, direction, stable_atr):
    buf        = stable_atr * MICRO_BREAK_BUFF
    last_close = float(df["close"].iloc[-1])

    if direction == 1:
        if len(int_sl) < 3 or len(int_sh) < 2:
            return False, "", 0
        l0, l1, l2  = int_sl[0][1], int_sl[1][1], int_sl[2][1]
        prior_bear   = l1 < l2
        higher_low   = l0 > l1
        broken_above = last_close > int_sh[0][1] + buf
        if prior_bear and higher_low and broken_above:
            return True, (f"CHOCH BUY:LL{l1:.0f}<{l2:.0f}"
                          f"->HL{l0:.0f}>{l1:.0f}"
                          f"->close>{int_sh[0][1]:.0f}"), 5
    elif direction == -1:
        if len(int_sh) < 3 or len(int_sl) < 2:
            return False, "", 0
        h0, h1, h2  = int_sh[0][1], int_sh[1][1], int_sh[2][1]
        prior_bull   = h1 > h2
        lower_high   = h0 < h1
        broken_below = last_close < int_sl[0][1] - buf
        if prior_bull and lower_high and broken_below:
            return True, (f"CHOCH SELL:HH{h1:.0f}>{h2:.0f}"
                          f"->LH{h0:.0f}<{h1:.0f}"
                          f"->close<{int_sl[0][1]:.0f}"), 5
    return False, "", 0


# ==============================================================================
# DISPLACEMENT
# ==============================================================================

def detect_displacement(df, stable_atr, direction):
    if len(df) < 3 or stable_atr <= 0:
        return "NONE", 0, "insufficient data"
    last_rng  = float(df["high"].iloc[-1] - df["low"].iloc[-1])
    last_body = abs(float(df["close"].iloc[-1]) - float(df["open"].iloc[-1]))
    body_r    = last_body / last_rng if last_rng > 0 else 0
    impulse   = last_rng > stable_atr * DISP_ATR_MULT and body_r > DISP_BODY_RATIO
    p_cl = float(df["close"].iloc[-2])
    p_op = float(df["open"].iloc[-2])
    c_cl = float(df["close"].iloc[-1])
    c_op = float(df["open"].iloc[-1])
    cont = ((direction == 1  and c_cl > c_op and p_cl > p_op and c_cl > p_cl) or
            (direction == -1 and c_cl < c_op and p_cl < p_op and c_cl < p_cl))
    fvgs   = detect_fvg(df, direction, lookback=5)
    fvg_ok = len(fvgs) > 0 and not fvgs[0]["filled"]
    score  = sum([impulse, cont, fvg_ok])
    detail = f"impulse={impulse} cont={cont} fvg={fvg_ok}"
    if score >= 3: return "STRONG",   3, f"STRONG({detail})"
    if score >= 2: return "MODERATE", 2, f"MODERATE({detail})"
    if score >= 1: return "WEAK",     1, f"WEAK({detail})"
    return "NONE", 0, f"none({detail})"


# ==============================================================================
# SWEEP + REJECTION
# ==============================================================================

def detect_sweep_rejection(df, pools, direction, stable_atr):
    if len(df) < 3:
        return False, "", 0
    last_high  = float(df["high"].iloc[-1])
    last_low   = float(df["low"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    last_open  = float(df["open"].iloc[-1])
    rng        = last_high - last_low
    if rng <= 0:
        return False, "", 0
    active = [p for p in pools if p.active and not p.swept]
    if direction == 1:
        lw    = min(last_open, last_close) - last_low
        ratio = lw / rng
        if ratio < SWEEP_WICK_RATIO:
            return False, "", 0
        for pool in active:
            if (pool.side == "low"
                    and last_low   < pool.price
                    and last_close > pool.price
                    and last_close > last_open):
                bonus = min(int(pool.strength() + 3), 7)
                return True, (f"Sweep+reject BUY:{pool.price:.0f}"
                              f"(wick={ratio:.0%},str={pool.strength():.1f})"), bonus
    else:
        uw    = last_high - max(last_open, last_close)
        ratio = uw / rng
        if ratio < SWEEP_WICK_RATIO:
            return False, "", 0
        for pool in active:
            if (pool.side == "high"
                    and last_high  > pool.price
                    and last_close < pool.price
                    and last_close < last_open):
                bonus = min(int(pool.strength() + 3), 7)
                return True, (f"Sweep+reject SELL:{pool.price:.0f}"
                              f"(wick={ratio:.0%},str={pool.strength():.1f})"), bonus
    return False, "", 0


# ==============================================================================
# FAILED CHOCH -- BREAKOUT THEN RECLAIM
# ==============================================================================

def detect_failed_choch(df, ext_sh, ext_sl, direction, stable_atr):
    if len(df) < 3:
        return False, ""
    buf        = stable_atr * MICRO_BREAK_BUFF
    prev_close = float(df["close"].iloc[-2])
    last_close = float(df["close"].iloc[-1])
    if direction == 1 and ext_sh:
        ref_h = ext_sh[0][1]
        if prev_close > ref_h + buf and last_close < ref_h - buf:
            return True, f"Failed CHOCH BUY:broke {ref_h:.2f} then below"
    elif direction == -1 and ext_sl:
        ref_l = ext_sl[0][1]
        if prev_close < ref_l - buf and last_close > ref_l + buf:
            return True, f"Failed CHOCH SELL:broke {ref_l:.2f} then above"
    return False, ""


# ==============================================================================
# FIX 3: ORDERBLOCK -- IMPULSE BODY RATIO FILTER
# ==============================================================================

def detect_orderblock(df, direction, stable_atr, ext_sh, ext_sl):
    """
    FIX 3: Impulse body ratio >= OB_IMPULSE_BODY_RATIO required.
    Weak displacement origins (body ratio < 0.70) are rejected.
    Also: BOS + FVG + unviolated OB body (from v9).
    """
    if len(df) < 5 or stable_atr <= 0:
        return None
    buf        = stable_atr * MICRO_BREAK_BUFF
    last_close = float(df["close"].iloc[-1])
    min_body   = stable_atr * MIN_OB_BODY_MULT

    bos_ok = False
    if direction == 1 and len(ext_sh) >= 2:
        bos_ok = last_close > ext_sh[1][1] + buf
    elif direction == -1 and len(ext_sl) >= 2:
        bos_ok = last_close < ext_sl[1][1] - buf
    if not bos_ok:
        return None

    fvgs   = detect_fvg(df, direction, lookback=10)
    fvg_ok = any(not f["filled"] for f in fvgs)
    if not fvg_ok:
        return None

    for i in range(len(df) - 2, max(0, len(df) - 25), -1):
        c_cl = float(df["close"].iloc[i])
        c_op = float(df["open"].iloc[i])
        body = abs(c_cl - c_op)
        if body < min_body:
            continue
        n_cl  = float(df["close"].iloc[i + 1])
        n_op  = float(df["open"].iloc[i + 1])
        n_rng = float(df["high"].iloc[i + 1] - df["low"].iloc[i + 1])
        if n_rng <= 0:
            continue

        # FIX 3: impulse body ratio must be strong
        impulse_body_ratio = abs(n_cl - n_op) / n_rng
        if impulse_body_ratio < OB_IMPULSE_BODY_RATIO:
            continue   # weak displacement origin -- not institutional

        if direction == 1:
            bearish_ob   = c_cl < c_op
            next_impulse = n_cl > n_op and n_rng > stable_atr * DISP_ATR_MULT
            if bearish_ob and next_impulse:
                ob_top = c_op
                ob_bot = c_cl
                later_low = float(df["low"].iloc[i + 1:].min())
                if later_low > ob_bot - buf:
                    return {"top": ob_top, "bottom": ob_bot,
                            "mid": (ob_top + ob_bot) / 2.0,
                            "time": df.index[i], "type": "bullish_ob"}
        else:
            bullish_ob   = c_cl > c_op
            next_impulse = n_cl < n_op and n_rng > stable_atr * DISP_ATR_MULT
            if bullish_ob and next_impulse:
                ob_top = c_cl
                ob_bot = c_op
                later_high = float(df["high"].iloc[i + 1:].max())
                if later_high < ob_top + buf:
                    return {"top": ob_top, "bottom": ob_bot,
                            "mid": (ob_top + ob_bot) / 2.0,
                            "time": df.index[i], "type": "bearish_ob"}
    return None


# ==============================================================================
# INDUCEMENT
# ==============================================================================

def detect_inducement(int_sh, int_sl, direction, stable_atr):
    if direction == 1 and len(int_sh) >= 2:
        diff = abs(int_sh[0][1] - int_sh[1][1])
        if diff < stable_atr * 0.5:
            return True, f"Inducement high {int_sh[0][1]:.2f}(weak swing)"
    elif direction == -1 and len(int_sl) >= 2:
        diff = abs(int_sl[0][1] - int_sl[1][1])
        if diff < stable_atr * 0.5:
            return True, f"Inducement low {int_sl[0][1]:.2f}(weak swing)"
    return False, ""


# ==============================================================================
# MANDATORY CONDITIONS
# ==============================================================================

def check_mandatory_conditions(ext_sh, ext_sl, regime, state, failed_choch):
    failures = []
    if not ext_sh or not ext_sl:
        failures.append("no confirmed external swings")
    if regime == "NEWS_SPIKE":
        failures.append("news spike -- structure unreliable")
    if failed_choch:
        failures.append("failed CHOCH -- trap active")
    if state and state.stage == SEQ_IDLE:
        failures.append("no structure sequence initiated")
    return (len(failures) == 0), failures


# ==============================================================================
# INVALIDATION
# ==============================================================================

def check_invalidation(ext_sh, ext_sl, df, direction, stable_atr):
    buf        = stable_atr * INVALIDATION_BUFF
    last_close = float(df["close"].iloc[-1])
    if direction == 1 and ext_sl:
        if last_close < ext_sl[0][1] - buf:
            return True, f"BUY invalidated: close below swing low {ext_sl[0][1]:.2f}"
    elif direction == -1 and ext_sh:
        if last_close > ext_sh[0][1] + buf:
            return True, f"SELL invalidated: close above swing high {ext_sh[0][1]:.2f}"
    return False, ""


# ==============================================================================
# FIX 5: DISPLACEMENT LEG -- LIFECYCLE MEMORY
# ==============================================================================

class DisplacementLeg:
    """
    FIX 5: Tracks impulse leg from origin to current state.
    Provides continuation_probability() for scoring.
    Stored in StructureState for persistence.
    """
    def __init__(self):
        self.origin_price  = None
        self.origin_time   = None
        self.extreme_price = None
        self.direction     = 0
        self.fvg_count     = 0
        self.candle_count  = 0
        self.active        = False
        self.efficiency    = 0.0
        self.retracement   = 0.0

    def start(self, price, time, direction, efficiency, fvg_count=0):
        self.origin_price  = price
        self.origin_time   = time
        self.direction     = direction
        self.efficiency    = efficiency
        self.fvg_count     = fvg_count
        self.active        = True
        self.candle_count  = 0
        self.extreme_price = price
        self.retracement   = 0.0

    def update(self, current_price):
        if not self.active or self.origin_price is None:
            return
        self.candle_count += 1
        if self.direction == 1:
            self.extreme_price = max(self.extreme_price, current_price)
            total_move = self.extreme_price - self.origin_price
            if total_move > 0:
                self.retracement = (self.extreme_price - current_price) / total_move
        else:
            self.extreme_price = min(self.extreme_price, current_price)
            total_move = self.origin_price - self.extreme_price
            if total_move > 0:
                self.retracement = (current_price - self.extreme_price) / total_move
        self.retracement = max(0.0, min(1.0, self.retracement))

    def continuation_probability(self):
        """Higher efficiency + lower retracement = higher continuation score."""
        if not self.active:
            return 0.0
        score = self.efficiency * (1.0 - self.retracement)
        if self.fvg_count > 0:
            score = min(1.0, score + 0.1 * self.fvg_count)
        return round(score, 3)

    def reset(self):
        self.active = False


# ==============================================================================
# FIX 2: SEQUENTIAL STATE MACHINE WITH HOLD CHECK
# ==============================================================================

class StructureState:
    """
    FIX 2: RETEST_REACTED now requires price to hold above/below FVG mid.
    Failure to hold resets state immediately.
    FIX 5: DisplacementLeg integrated for lifecycle memory.
    is_trading_permitted() lets live_bot enforce minimum stage.
    """

    def __init__(self):
        self.displacement_leg = DisplacementLeg()
        self.reset()

    def reset(self):
        self.stage            = SEQ_IDLE
        self.direction        = 0
        self.sweep_time       = None
        self.displacement     = "NONE"
        self.bos_confirmed    = False
        self.candles_in_stage = 0
        self.pools            = []
        self.fvgs             = []
        self.displacement_leg.reset()

    def advance(self, new_stage, direction=0):
        if direction != 0:
            self.direction = direction
        self.stage            = new_stage
        self.candles_in_stage = 0

    def tick(self):
        self.candles_in_stage += 1
        if self.candles_in_stage > STATE_MAX_AGE:
            self.reset()

    def is_trading_permitted(self, min_stage=SEQ_BOS):
        """
        live_bot.py calls this before any trade execution.
        Recommended minimum:
          SEQ_BOS            -- moderate quality
          SEQ_RETEST         -- high quality
          SEQ_CONFIRMED      -- maximum quality
        """
        try:
            cur = SEQ_ORDER.index(self.stage)
            mn  = SEQ_ORDER.index(min_stage)
            return cur >= mn
        except ValueError:
            return False

    def sequence_bonus(self):
        stage_scores = {
            SEQ_IDLE: 0, SEQ_SWEEP: 1, SEQ_DISPLACE: 2,
            SEQ_BOS: 3, SEQ_RETEST: 4,
            SEQ_RETEST_REACTED: 5, SEQ_CONFIRMED: 5,
        }
        return stage_scores.get(self.stage, 0)

    def update(self, df, ext_sh, ext_sl, direction, stable_atr,
               sweep_ok, bos_basic, bos_strong, disp_strength,
               current_price, de_ratio=0.5):
        """
        FIX 2: SEQ_RETEST_REACTED requires hold confirmation.
        FIX 5: DisplacementLeg updated on each candle.
        """
        self.tick()
        d = _norm(direction)

        for pool in self.pools:
            pool.update(df)

        if not self.pools or self.candles_in_stage > 20:
            self.pools = build_liquidity_pools(ext_sh, ext_sl)

        self.fvgs = detect_fvg(df, d, lookback=30)

        # FIX 5: update displacement leg lifecycle
        self.displacement_leg.update(current_price)

        if self.stage == SEQ_IDLE:
            if sweep_ok:
                self.advance(SEQ_SWEEP, d)
                self.sweep_time = df.index[-1]

        elif self.stage == SEQ_SWEEP:
            if disp_strength in ("STRONG", "MODERATE"):
                self.advance(SEQ_DISPLACE)
                self.displacement = disp_strength
                # FIX 5: start tracking the displacement leg
                fvg_count = len([f for f in self.fvgs if not f["filled"]])
                self.displacement_leg.start(
                    current_price, df.index[-1], d, de_ratio, fvg_count
                )
            elif self.candles_in_stage > 10:
                self.reset()

        elif self.stage == SEQ_DISPLACE:
            if bos_basic:
                self.advance(SEQ_BOS)
                self.bos_confirmed = True
            elif self.candles_in_stage > 15:
                self.reset()

        elif self.stage == SEQ_BOS:
            nearest = nearest_unmitigated_fvg(self.fvgs, current_price, stable_atr)
            if nearest:
                self.advance(SEQ_RETEST)
            elif self.candles_in_stage > 20:
                self.reset()

        elif self.stage == SEQ_RETEST:
            rejection = _detect_rejection_candle(df, d)
            if rejection:
                self.advance(SEQ_RETEST_REACTED)
            elif self.candles_in_stage > 10:
                self.reset()

        elif self.stage == SEQ_RETEST_REACTED:
            # FIX 2: price must HOLD above/below FVG mid after rejection
            hold_ok  = False
            nearest  = nearest_unmitigated_fvg(self.fvgs, current_price, stable_atr)
            if nearest:
                if d == 1:
                    hold_ok = current_price > nearest["mid"]
                else:
                    hold_ok = current_price < nearest["mid"]
            else:
                # Fall back to BOS level hold check
                if d == 1 and len(ext_sh) >= 2:
                    hold_ok = current_price > ext_sh[1][1]
                elif d == -1 and len(ext_sl) >= 2:
                    hold_ok = current_price < ext_sl[1][1]

            if hold_ok:
                self.advance(SEQ_CONFIRMED)
            else:
                self.reset()   # FIX 2: failed to hold -- dead-cat bounce filtered

        elif self.stage == SEQ_CONFIRMED:
            if self.candles_in_stage > 2:
                self.reset()


# ==============================================================================
# SESSION MEMORY
# ==============================================================================

def get_session_memory(df):
    if df is None or len(df) < 20:
        return {}
    df_c = df.copy()
    try:
        df_c.index = pd.to_datetime(df_c.index, utc=True)
    except Exception:
        try:
            df_c.index = (df_c.index.tz_localize("UTC")
                          if df_c.index.tzinfo is None
                          else df_c.index.tz_convert("UTC"))
        except Exception:
            df_c.index = pd.to_datetime(df_c.index)
    latest    = df_c.index[-1]
    df_recent = df_c[df_c.index >= latest - pd.Timedelta(hours=24)]
    if len(df_recent) < 4:
        return {}
    hour   = df_recent.index.hour
    result = {}
    for name, mask in [
        ("asia",   (hour >= 22) | (hour < 7)),
        ("london", (hour >= 7)  & (hour < 13)),
        ("ny",     (hour >= 13) & (hour < 22)),
    ]:
        bars = df_recent[mask]
        if len(bars) >= 3:
            result[f"{name}_high"] = float(bars["high"].max())
            result[f"{name}_low"]  = float(bars["low"].min())
    return result


def get_weekly_levels(df):
    if df is None or len(df) < 50:
        return {}
    df_c       = df.copy()
    df_c.index = pd.to_datetime(df_c.index)
    try:
        df_c["iso_week"] = df_c.index.isocalendar().week.values
    except AttributeError:
        df_c["iso_week"] = df_c.index.to_series().dt.isocalendar().week.values
    cw        = int(df_c["iso_week"].iloc[-1])
    week_bars = df_c[df_c["iso_week"] == cw]
    if len(week_bars) < 10:
        return {}
    return {"weekly_high": float(week_bars["high"].max()),
            "weekly_low":  float(week_bars["low"].min())}


def get_prev_day_levels(df):
    if df is None or len(df) < 100:
        return {}
    df_c           = df.copy()
    df_c.index     = pd.to_datetime(df_c.index)
    df_c["date"]   = df_c.index.date
    days           = df_c["date"].unique()
    if len(days) < 2:
        return {}
    prev = df_c[df_c["date"] == days[-2]]
    if len(prev) == 0:
        return {}
    return {"prev_high":  float(prev["high"].max()),
            "prev_low":   float(prev["low"].min()),
            "prev_close": float(prev["close"].iloc[-1])}


def get_round_numbers(price, step=ROUND_STEP, count=4):
    base = round(price / step) * step
    return [base + i * step for i in range(-count, count + 1)]


def is_near(price, level, atr, factor=PROXIMITY_FACTOR):
    if not level:
        return False
    return abs(price - level) <= atr * factor


# ==============================================================================
# MAIN STRUCTURE SCORE
# ==============================================================================

def get_structure_score(df,
                         current_price,
                         direction,
                         raw_atr,
                         htf_bias=None,
                         news_active=False,
                         state=None,
                         session="OVERLAP"):
    """
    Full institutional structure score -- v10 all refinements applied.

    Parameters:
      df           : M15 OHLCV (250 bars recommended)
      current_price: current bid price
      direction    : 1=BUY, -1=SELL, 2=SELL(legacy)
      raw_atr      : ATR from live_bot features
      htf_bias     : {"H1":"BULL","H1_phase":"PULLBACK",
                       "H4":"BULL","H4_phase":"IMPULSE"}
      news_active  : True suppresses all structure scoring
      state        : StructureState instance -- created ONCE in live_bot.py
      session      : "ASIA"/"LONDON"/"OVERLAP"/"NY"

    Returns (score: int, detail: str)
    Score always clamped to [-10, +10].
    """
    d = _norm(direction)

    if news_active:
        return 0, "Structure suppressed: news window"

    stable_atr = get_stable_atr(df, raw_atr)
    sb         = SESSION_BEHAVIOR.get(session, SESSION_BEHAVIOR["OVERLAP"])

    # FIX 6: Regime includes VoV
    regime = detect_market_regime(df, stable_atr)
    rw     = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["EXPANDING"])

    # Fractal swings
    ext_sh = detect_swing_highs(df, SWING_LOOKBACK_EXT)
    ext_sl = detect_swing_lows(df,  SWING_LOOKBACK_EXT)
    int_sh = detect_internal_highs(df)
    int_sl = detect_internal_lows(df)

    # FVG + Premium/Discount
    fvgs        = detect_fvg(df, d, FVG_LOOKBACK)
    nearest_fvg = nearest_unmitigated_fvg(fvgs, current_price, stable_atr)
    pd_info     = get_premium_discount(ext_sh, ext_sl, current_price)

    # Pools
    if state and state.pools:
        pools = state.pools
        for p in pools:
            p.update(df)
    else:
        pools = build_liquidity_pools(ext_sh, ext_sl, session)

    session_mem = get_session_memory(df)
    weekly_mem  = get_weekly_levels(df)
    prev_day    = get_prev_day_levels(df)
    round_nums  = get_round_numbers(current_price)

    # Trap detection
    failed_choch, fc_det  = detect_failed_choch(df, ext_sh, ext_sl, d, stable_atr)
    inducement_ok, ind_dt = detect_inducement(int_sh, int_sl, d, stable_atr)

    # Mandatory gate
    permitted, rejections = check_mandatory_conditions(
        ext_sh, ext_sl, regime, state, failed_choch
    )

    details = [f"regime={regime}", f"session={sb['style']}"]

    if not permitted:
        for r in rejections:
            details.append(f"MANDATORY FAIL:{r}")
        return max(-MAX_STRUCT_SCORE, -8), " | ".join(details)

    # Invalidation
    inv_ok, inv_rsn = check_invalidation(ext_sh, ext_sl, df, d, stable_atr)
    if inv_ok:
        details.append(inv_rsn)
        return max(-MAX_STRUCT_SCORE, -8), " | ".join(details)

    # Displacement
    disp_str, disp_bon, disp_det = detect_displacement(df, stable_atr, d)
    displacement_occurred = disp_str in ("STRONG", "MODERATE")

    # DE ratio for DisplacementLeg
    de_ratio = directional_efficiency(df["close"], 20)

    # Sweep + rejection
    sweep_ok, sweep_rsn, sweep_bon = detect_sweep_rejection(df, pools, d, stable_atr)

    # BOS (FIX 1: body break + strong continuation)
    bos_basic, bos_strong = detect_bos(ext_sh, ext_sl, df, d, stable_atr)

    # CHOCH on internal swings
    choch_ok, choch_rsn, choch_bon = detect_choch(int_sh, int_sl, df, d, stable_atr)

    # OB (FIX 3: impulse quality filter)
    ob = detect_orderblock(df, d, stable_atr, ext_sh, ext_sl)

    # OTE (only after displacement)
    ote_ok, ote_det, ote_bon = detect_ote_zone(
        ext_sh, ext_sl, current_price, d, displacement_occurred
    )

    # FIX 7: Orderflow proxy
    of_result = compute_orderflow_proxy(df, stable_atr)

    # Update state machine
    if state:
        state.update(df, ext_sh, ext_sl, d, stable_atr,
                     sweep_ok, bos_basic, bos_strong, disp_str,
                     current_price, de_ratio)

    if inducement_ok:
        details.append(f"WARNING:{ind_dt}")

    # FIX 5: Displacement leg continuation probability
    cont_prob = 0.0
    if state and state.displacement_leg.active:
        cont_prob = state.displacement_leg.continuation_probability()
        if cont_prob > 0.6:
            details.append(f"impulse_cont={cont_prob:.0%}(strong)")
        elif cont_prob > 0.3:
            details.append(f"impulse_cont={cont_prob:.0%}")

    # ==================================================================
    # BUCKET A: REVERSAL (cap=REVERSAL_CAP)
    # ==================================================================
    rev_scores = []
    if choch_ok:
        pts = choch_bon * rw["choch"] * sb.get("choch_mult", 1.0)
        if disp_str == "STRONG":
            pts += 1.0
        rev_scores.append(pts)
        details.append(choch_rsn)
    if sweep_ok:
        rev_scores.append(sweep_bon * rw["sweep"] * sb.get("sweep_mult", 1.0))
        details.append(sweep_rsn)
    reversal_pts = min(_diminishing_sum(rev_scores), REVERSAL_CAP)

    # ==================================================================
    # BUCKET B: CONTINUATION (cap=CONTINUATION_CAP)
    # FIX 7: orderflow proxy added as correlated evidence component
    # ==================================================================
    cont_scores = []
    if bos_basic:
        base_bos = 3.0 + disp_bon
        if bos_strong:
            base_bos += 1.0
            details.append(f"BOS STRONG+{disp_str}")
        else:
            details.append(f"BOS basic+{disp_str}")
        cont_scores.append(base_bos * rw["bos"] * sb.get("bos_mult", 1.0))

    if nearest_fvg:
        cont_scores.append(3.0)
        details.append(f"FVG nearby({nearest_fvg['mid']:.2f})")

    if ob:
        ob_dist = abs(current_price - ob["mid"])
        if ob_dist <= stable_atr * 1.5:
            cont_scores.append(2.0 * sb.get("ob_mult", 1.0))
            details.append(f"OB({ob['mid']:.2f})")

    # FIX 7: orderflow as continuation evidence
    if of_result["score"] >= 6:
        cont_scores.append(2.0)
        details.append(f"orderflow STRONG({of_result['detail']})")
    elif of_result["score"] >= 3:
        cont_scores.append(1.0)
        details.append(f"orderflow moderate")

    # FIX 5: impulse continuation bonus
    if cont_prob > 0.6:
        cont_scores.append(2.0)
    elif cont_prob > 0.3:
        cont_scores.append(1.0)

    # Session breakouts
    sess_scores = []
    if d == 1:
        ah = session_mem.get("asia_high")
        lh = session_mem.get("london_high")
        if ah and current_price > ah + stable_atr * 0.3:
            sess_scores.append(3.0 * rw["session"])
            details.append("Asia high break(bull)")
        if lh and current_price > lh + stable_atr * 0.2:
            sess_scores.append(2.0 * rw["session"])
            details.append("London high break")
    else:
        al = session_mem.get("asia_low")
        ll = session_mem.get("london_low")
        if al and current_price < al - stable_atr * 0.3:
            sess_scores.append(3.0 * rw["session"])
            details.append("Asia low break(bear)")
        if ll and current_price < ll - stable_atr * 0.2:
            sess_scores.append(2.0 * rw["session"])
            details.append("London low break")

    all_cont     = cont_scores + sess_scores
    continuation_pts = min(_diminishing_sum(all_cont), CONTINUATION_CAP)

    # ==================================================================
    # BUCKET C: LIQUIDITY (cap=LIQUIDITY_CAP)
    # ==================================================================
    liquidity_pts = 0.0
    if not sweep_ok:
        active = [p for p in pools if p.active and not p.swept]
        if d == 1:
            for pool in active:
                if (pool.side == "low"
                        and current_price > pool.price
                        and pool.price > current_price - stable_atr * 2):
                    liquidity_pts += min(pool.strength(), 3)
                    details.append(f"eq-low cleared(str={pool.strength():.1f})")
                    break
        else:
            for pool in active:
                if (pool.side == "high"
                        and current_price < pool.price
                        and pool.price < current_price + stable_atr * 2):
                    liquidity_pts += min(pool.strength(), 3)
                    details.append(f"eq-high cleared(str={pool.strength():.1f})")
                    break
    liquidity_pts = min(liquidity_pts, LIQUIDITY_CAP)

    # ==================================================================
    # BUCKET D: CONTEXT with time decay
    # ==================================================================
    context_bonus   = 0.0
    context_penalty = 0.0

    recent_high = ext_sh[0] if ext_sh else None
    recent_low  = ext_sl[0] if ext_sl else None

    if d == 1:
        if recent_high:
            age_h = _swing_age_candles(recent_high[0], df)
            if is_near(current_price, recent_high[1], stable_atr, 0.8):
                context_penalty += _time_decay(5.0, age_h)
                details.append(f"near ext high resistance(age={age_h:.0f}c)")
        if recent_low:
            age_l = _swing_age_candles(recent_low[0], df)
            if is_near(current_price, recent_low[1], stable_atr, 1.2):
                context_bonus += _time_decay(3.0, age_l)
                details.append(f"near ext low support(age={age_l:.0f}c)")
    else:
        if recent_low:
            age_l = _swing_age_candles(recent_low[0], df)
            if is_near(current_price, recent_low[1], stable_atr, 0.8):
                context_penalty += _time_decay(5.0, age_l)
                details.append(f"near ext low support(age={age_l:.0f}c)")
        if recent_high:
            age_h = _swing_age_candles(recent_high[0], df)
            if is_near(current_price, recent_high[1], stable_atr, 1.2):
                context_bonus += _time_decay(3.0, age_h)
                details.append(f"near ext high resistance(age={age_h:.0f}c)")

    ph = prev_day.get("prev_high")
    pl = prev_day.get("prev_low")
    if d == 1:
        if ph:
            if is_near(current_price, ph, stable_atr, 0.5):
                context_penalty += 4.0; details.append("near prev day high")
            elif current_price > ph:
                context_bonus += 4.0; details.append("above prev day high(bull)")
    else:
        if pl:
            if is_near(current_price, pl, stable_atr, 0.5):
                context_penalty += 4.0; details.append("near prev day low")
            elif current_price < pl:
                context_bonus += 4.0; details.append("below prev day low(bear)")

    wh = weekly_mem.get("weekly_high")
    wl = weekly_mem.get("weekly_low")
    if d == 1 and wh:
        if is_near(current_price, wh, stable_atr, 0.4):
            context_penalty += 4.0; details.append("near weekly high(extended)")
        elif current_price > wh:
            context_bonus += 2.0; details.append("weekly high breakout")
    elif d == -1 and wl:
        if is_near(current_price, wl, stable_atr, 0.4):
            context_penalty += 4.0; details.append("near weekly low(extended)")
        elif current_price < wl:
            context_bonus += 2.0; details.append("weekly low breakdown")

    context_bonus = min(context_bonus, CONTEXT_CAP)

    round_prox = min(stable_atr * 0.3, 2.0)
    at_round   = any(abs(current_price - rn) <= round_prox for rn in round_nums)
    if at_round:
        context_penalty += 3.0; details.append("at round number barrier")
    else:
        context_bonus += 2.0; details.append("clear of round numbers")

    pd_zone = pd_info.get("zone", "UNKNOWN")
    if d == 1:
        if pd_zone == "DISCOUNT":
            context_bonus += 3.0; details.append(f"BUY DISCOUNT({pd_info.get('pct',0):.0%})")
        elif pd_zone == "PREMIUM":
            context_penalty += 3.0; details.append("BUY in PREMIUM(adverse)")
    else:
        if pd_zone == "PREMIUM":
            context_bonus += 3.0; details.append(f"SELL PREMIUM({pd_info.get('pct',0):.0%})")
        elif pd_zone == "DISCOUNT":
            context_penalty += 3.0; details.append("SELL in DISCOUNT(adverse)")

    if ote_ok:
        context_bonus += ote_bon; details.append(ote_det)

    htf_bonus, htf_penalty, htf_det = evaluate_htf_phase(htf_bias, d)
    details.append(htf_det)

    seq_bonus = 0
    if state:
        seq_bonus = state.sequence_bonus()
        if seq_bonus > 0:
            details.append(f"sequence={state.stage}(+{seq_bonus})")

    # Combine
    raw = (reversal_pts + continuation_pts + liquidity_pts
           + context_bonus - context_penalty
           + htf_bonus - htf_penalty
           + seq_bonus)

    score = max(-MAX_STRUCT_SCORE, min(MAX_STRUCT_SCORE, round(raw)))
    return score, " | ".join(details)


# ==============================================================================
# SCALP MICRO-BREAKOUT LEVELS
# ==============================================================================

def scalp_break_buy_level(df, atr):
    high3 = float(df["high"].shift(1).rolling(3).max().iloc[-1])
    return round(high3 + atr * MICRO_BREAK_BUFF, 2)


def scalp_break_sell_level(df, atr):
    low3 = float(df["low"].shift(1).rolling(3).min().iloc[-1])
    return round(low3 - atr * MICRO_BREAK_BUFF, 2)