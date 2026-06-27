"""
CopetraNova -- feature_engine.py
==================================
Builds the training dataset from M15 XAUUSD data.

Strategy:
  - Trains on M15 only (4 years of clean history)
  - M5 is used LIVE in live_bot.py as a real-time confirmation gate
  - This avoids the M5 data shortage problem (MT5 only gave 17 months of M5)
  - Result: 4 years x 15 strong features = reliable, stable model

What this file does:
  1. Loads M15 CSV from data_engine.py
  2. Computes 15 technical features on M15
  3. Labels each candle BUY / SELL / HOLD using 6-pip spread-aware threshold
  4. Filters to 2022-06-01 onwards (stable Gold regime, avoids COVID noise)
  5. Saves training_dataset.csv ready for train_model.py

Run:
    python feature_engine.py

Output:
    data/training_dataset.csv
    data/feature_report.txt
"""

import os
import pandas as pd
import numpy as np

# -- paths ---------------------------------------------------------------------
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
RAW_DIR    = os.path.join(DATA_DIR, "raw")
OUT_CSV    = os.path.join(DATA_DIR, "training_dataset.csv")
OUT_REPORT = os.path.join(DATA_DIR, "feature_report.txt")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(RAW_DIR,  exist_ok=True)

# -- config --------------------------------------------------------------------
PIP_SIZE    = 0.1      # 1 pip for XAUUSD = $0.10
SPREAD_PIPS = 3        # typical broker spread
TARGET_PIPS = 6        # minimum profitable move after spread
THRESHOLD   = (TARGET_PIPS + SPREAD_PIPS) * PIP_SIZE   # $0.90

# 4 years of M15 data -- avoids early COVID volatility spike
# M15 from MT5 goes back to Feb 2022, so we start Jun 2022 for clean data
TRAIN_START = "2022-06-01"
TRAIN_END   = "2026-12-31"


# ==============================================================================
# SECTION 1 -- DATA LOADING
# ==============================================================================

def load_ohlcv(filepath: str, label: str) -> pd.DataFrame:
    """Load MT5 CSV export into a clean OHLCV DataFrame."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(
            f"\n[CopetraNova] {label} file not found: {filepath}\n"
            f"  Run data_engine.py first.\n"
        )

    df = pd.read_csv(filepath)
    df.columns = [c.strip().lower() for c in df.columns]

    # handle MT5 column naming
    if "tick_volume" in df.columns and "volume" not in df.columns:
        df.rename(columns={"tick_volume": "volume"}, inplace=True)

    required = ["time", "open", "high", "low", "close"]
    for col in required:
        if col not in df.columns:
            raise ValueError(
                f"[CopetraNova] Column '{col}' missing in {filepath}.\n"
                f"  Found: {list(df.columns)}"
            )

    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    df = df[~df.index.duplicated(keep="last")]

    print(f"  [load] {label}: {len(df):,} candles  "
          f"({df.index.min().date()} to {df.index.max().date()})")
    return df


# ==============================================================================
# SECTION 2 -- INDICATOR HELPERS (zero look-ahead)
# ==============================================================================

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range -- measures volatility."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr_percentile(atr_series: pd.Series, window: int = 100) -> pd.Series:
    """
    Rolling percentile rank of ATR over the last `window` bars.
    0 = lowest volatility seen, 100 = highest volatility seen.
    Used by live_bot screener to skip dead or explosive markets.
    """
    return atr_series.rolling(window).rank(pct=True) * 100


# ==============================================================================
# SECTION 3 -- FEATURE COMPUTATION (15 M15 features)
# ==============================================================================

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute 15 technical features on M15 OHLCV data.

    All features reference only CLOSED candles -- no look-ahead bias.
    Features are designed to capture:
      - Price structure  (body, wicks, range, position)
      - Trend direction  (EMA gaps)
      - Momentum         (rate of change, acceleration)
      - Volatility       (ATR, ATR percentile)
      - Mean reversion   (RSI, price vs EMA)

    These 15 were selected after importance analysis -- they are the ones
    the model actually uses. The full 35-feature set was tried but the
    extra M5/cross-TF features were dropped by the model due to limited
    M5 history. This clean 15-feature set is more reliable.
    """
    feat = pd.DataFrame(index=df.index)

    c = df["close"]
    o = df["open"]
    h = df["high"]
    l = df["low"]

    # -- price structure -------------------------------------------------------
    body       = (c - o).abs()
    rng        = h - l
    upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
    lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l

    feat["body"]        = body
    feat["range"]       = rng
    feat["upper_wick"]  = upper_wick
    feat["lower_wick"]  = lower_wick
    feat["price_pos"]   = (c - l) / rng.replace(0, np.nan)
    # price_pos: 1.0 = closed at high, 0.0 = closed at low

    # -- trend -----------------------------------------------------------------
    _ema9   = ema(c, 9)
    _ema21  = ema(c, 21)
    _ema50  = ema(c, 50)
    _ema200 = ema(c, 200)

    feat["ema_gap_fast"]   = _ema9  - _ema21    # fast trend: +ve = bullish
    feat["ema_gap_slow"]   = _ema50 - _ema200   # slow trend: +ve = bull market
    feat["price_vs_ema50"] = c - _ema50         # distance from mid trend
    feat["price_vs_ema200"]= c - _ema200        # distance from macro trend

    # -- momentum --------------------------------------------------------------
    feat["momentum"]     = c - c.shift(5)               # 5-bar momentum
    feat["acceleration"] = feat["momentum"] - feat["momentum"].shift(1)
    # acceleration: is momentum speeding up or slowing down?

    # -- volatility ------------------------------------------------------------
    _atr = atr(df, 14)
    feat["atr"]        = _atr
    feat["volatility"] = _atr / c.replace(0, np.nan)   # normalised ATR
    feat["atr_pct"]    = atr_percentile(_atr, 100)      # 0-100 percentile rank

    # -- oscillator ------------------------------------------------------------
    feat["rsi"] = rsi(c, 14)

    return feat


# ==============================================================================
# SECTION 4 -- SPREAD-AWARE LABELLING
# ==============================================================================

def label_candles(df: pd.DataFrame, threshold: float) -> pd.Series:
    """
    Label each M15 candle based on what the NEXT candle's close did.

    BUY  (1): next close rose more than `threshold` above current close
    SELL (2): next close fell more than `threshold` below current close
    HOLD (0): move was smaller than threshold -- not worth trading

    Why next candle? We want to know if the signal generated at candle close
    would have been profitable on the following candle. This is the correct
    way to label for a 1-candle-ahead prediction task.

    threshold = (target_pips + spread_pips) * pip_size
              = (6 + 3) * 0.10 = $0.90
    This ensures every BUY/SELL label represents a move large enough to
    cover spread AND deliver a net profit.
    """
    next_close   = df["close"].shift(-1)
    price_change = next_close - df["close"]

    labels = pd.Series(0, index=df.index, name="label")
    labels[price_change >  threshold] = 1   # BUY
    labels[price_change < -threshold] = 2   # SELL

    return labels


# ==============================================================================
# SECTION 5 -- MAIN PIPELINE
# ==============================================================================

def build_dataset(m15_path: str = None) -> pd.DataFrame:
    """
    Full pipeline:
      load M15 -> compute 15 features -> label -> filter -> save
    """
    m15_path = m15_path or os.path.join(RAW_DIR, "XAUUSD_M15.csv")

    print("\n[CopetraNova] feature_engine.py starting...\n")

    # 1. load
    print("Loading raw data...")
    df_m15 = load_ohlcv(m15_path, "M15")

    # 2. compute features
    print("\nComputing M15 features (15 features)...")
    features = compute_features(df_m15)

    # 3. label
    print(f"Labelling with {TARGET_PIPS}-pip target + {SPREAD_PIPS}-pip spread"
          f" = ${THRESHOLD:.2f} threshold...")
    labels = label_candles(df_m15, THRESHOLD)

    # 4. merge
    dataset = pd.concat([features, labels], axis=1)
    dataset.dropna(inplace=True)

    # 5. filter to training window
    dataset = dataset.loc[TRAIN_START:TRAIN_END]

    # drop last row (no future close for labelling)
    dataset = dataset.iloc[:-1]

    # 6. class balance report
    counts = dataset["label"].value_counts().sort_index()
    total  = len(dataset)
    label_names = {0: "HOLD", 1: "BUY", 2: "SELL"}

    print("\n" + "=" * 50)
    print("CopetraNova -- Feature Engine Report")
    print("=" * 50)
    print(f"Training window : {TRAIN_START} to {TRAIN_END}")
    print(f"Total samples   : {total:,}")
    print(f"Threshold       : ${THRESHOLD:.2f}  "
          f"({TARGET_PIPS} pip target + {SPREAD_PIPS} pip spread)")
    print(f"Features        : {features.shape[1]}")
    print()
    print("Class balance:")
    for code, name in label_names.items():
        n   = counts.get(code, 0)
        pct = 100 * n / total if total > 0 else 0
        print(f"  {name:4s} ({code}): {n:6,}  ({pct:.1f}%)")

    print("\nFeature list:")
    for col in features.columns:
        print(f"  {col}")

    # write report
    report_lines = [
        "CopetraNova -- Feature Engine Report",
        "=" * 50,
        f"Training window : {TRAIN_START} to {TRAIN_END}",
        f"Total samples   : {total:,}",
        f"Threshold       : ${THRESHOLD:.2f} "
          f"({TARGET_PIPS} pip target + {SPREAD_PIPS} pip spread)",
        f"Features        : {features.shape[1]}",
        "",
        "Class balance:",
    ]
    for code, name in label_names.items():
        n   = counts.get(code, 0)
        pct = 100 * n / total if total > 0 else 0
        report_lines.append(f"  {name} ({code}): {n:,} ({pct:.1f}%)")

    report_lines += ["", "Feature list:"]
    for col in features.columns:
        report_lines.append(f"  {col}")

    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    # 7. save dataset
    dataset.to_csv(OUT_CSV)

    print(f"\n[CopetraNova] Dataset saved  -> {OUT_CSV}")
    print(f"[CopetraNova] Report saved   -> {OUT_REPORT}")
    print(f"[CopetraNova] Done. {total:,} labelled candles ready for train_model.py\n")

    return dataset


# ==============================================================================
# FEATURE COLUMN LIST (imported by train_model.py and live_bot.py)
# ==============================================================================

FEATURE_COLS = [
    "body", "range", "upper_wick", "lower_wick", "price_pos",
    "ema_gap_fast", "ema_gap_slow", "price_vs_ema50", "price_vs_ema200",
    "momentum", "acceleration", "atr", "volatility", "atr_pct", "rsi",
]

# M5 confirmation is done LIVE in live_bot.py -- not as a training feature
# This keeps the model trained on 4 years of clean M15 data


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    import sys
    m15 = sys.argv[1] if len(sys.argv) > 1 else None
    build_dataset(m15)