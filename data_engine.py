"""
CopetraNova — data_engine.py  (bulletproof version)
=====================================================
Tries THREE different MT5 download methods in order.
If one fails, automatically tries the next.

Method 1: copy_rates_from_pos  (from position, no date needed)
Method 2: copy_rates_from      (from a specific past date)
Method 3: MT5 CSV export guide (manual fallback with instructions)

Run:
    python data_engine.py

Requirements:
    - MetaTrader 5 must be OPEN and LOGGED IN
    - pip install MetaTrader5 pandas
"""

import os
import sys
import time
import pandas as pd
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    print("\n[CopetraNova] ERROR: MetaTrader5 not installed.")
    print("  Run: pip install MetaTrader5\n")
    sys.exit(1)

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR  = os.path.join(BASE_DIR, "data", "raw")
os.makedirs(RAW_DIR, exist_ok=True)

# ── config ────────────────────────────────────────────────────────────────────
SYMBOL    = "XAUUSD"
BAR_COUNT = 150_000          # 150k bars — enough for 3+ years on M5

TIMEFRAMES = {
    "M5":  (mt5.TIMEFRAME_M5,  "M5"),
    "M15": (mt5.TIMEFRAME_M15, "M15"),
}


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

def connect_mt5() -> bool:
    print("[CopetraNova] Connecting to MetaTrader 5...")

    # Try up to 3 times — sometimes MT5 needs a moment
    for attempt in range(3):
        if mt5.initialize():
            break
        print(f"  Attempt {attempt+1} failed, retrying in 2s...")
        time.sleep(2)
    else:
        err = mt5.last_error()
        print(f"\n  ERROR: Cannot connect. Code={err[0]}  {err[1]}")
        print("  Make sure MetaTrader 5 is OPEN and LOGGED IN.\n")
        return False

    info = mt5.terminal_info()
    acct = mt5.account_info()
    print(f"  Terminal : {info.name}  (build {info.build})")
    print(f"  Account  : {acct.login}  ({acct.server})")
    print(f"  Balance  : {acct.currency} {acct.balance:,.2f}")
    print(f"  Connected: OK\n")
    return True


def ensure_symbol_visible() -> bool:
    sym = mt5.symbol_info(SYMBOL)
    if sym is None:
        print(f"  ERROR: '{SYMBOL}' not found. Check symbol name in your broker.")
        return False
    if not sym.visible:
        mt5.symbol_select(SYMBOL, True)
        time.sleep(1)
        print(f"  [{SYMBOL}] Added to Market Watch")
    print(f"  [{SYMBOL}] Digits={sym.digits}  Spread={sym.spread}\n")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD METHODS
# ══════════════════════════════════════════════════════════════════════════════

def method1_from_pos(tf_const: int, tf_name: str) -> pd.DataFrame | None:
    """Method 1: copy_rates_from_pos — reads from stored local history."""
    print(f"  [Method 1] copy_rates_from_pos({SYMBOL}, {tf_name}, 0, {BAR_COUNT:,})")

    # warm-up ping
    mt5.copy_rates_from_pos(SYMBOL, tf_const, 0, 10)
    time.sleep(0.5)

    rates = mt5.copy_rates_from_pos(SYMBOL, tf_const, 0, BAR_COUNT)
    if rates is not None and len(rates) > 100:
        return _to_df(rates)

    err = mt5.last_error()
    print(f"  [Method 1] Failed: {err}")
    return None


def method2_from_date(tf_const: int, tf_name: str) -> pd.DataFrame | None:
    """Method 2: copy_rates_from — pull from a fixed past date."""
    from_dt = datetime(2022, 1, 1, tzinfo=timezone.utc)
    print(f"  [Method 2] copy_rates_from({SYMBOL}, {tf_name}, {from_dt.date()}, {BAR_COUNT:,})")

    # trigger history load
    mt5.copy_rates_from(SYMBOL, tf_const, from_dt, 1)
    time.sleep(1)

    rates = mt5.copy_rates_from(SYMBOL, tf_const, from_dt, BAR_COUNT)
    if rates is not None and len(rates) > 100:
        return _to_df(rates)

    err = mt5.last_error()
    print(f"  [Method 2] Failed: {err}")
    return None


def method3_range_chunks(tf_const: int, tf_name: str) -> pd.DataFrame | None:
    """
    Method 3: pull in 6-month chunks using copy_rates_range.
    Useful when MT5 refuses large single requests.
    """
    print(f"  [Method 3] Chunked copy_rates_range for {SYMBOL} {tf_name}")

    end   = datetime.now(tz=timezone.utc)
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    chunks = []

    chunk_start = start
    while chunk_start < end:
        # 6-month window
        chunk_end = min(
            datetime(chunk_start.year + (chunk_start.month > 6),
                     ((chunk_start.month + 5) % 12) + 1, 1,
                     tzinfo=timezone.utc),
            end
        )
        rates = mt5.copy_rates_range(SYMBOL, tf_const, chunk_start, chunk_end)
        if rates is not None and len(rates) > 0:
            chunks.append(_to_df(rates))
            print(f"    Chunk {chunk_start.date()} → {chunk_end.date()}: "
                  f"{len(rates):,} bars")
        else:
            err = mt5.last_error()
            print(f"    Chunk {chunk_start.date()} → {chunk_end.date()}: "
                  f"EMPTY ({err})")
        chunk_start = chunk_end
        time.sleep(0.3)

    if chunks:
        df = pd.concat(chunks)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        return df

    print(f"  [Method 3] All chunks empty.")
    return None


def _to_df(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)
    keep = ["open", "high", "low", "close", "tick_volume"]
    return df[[c for c in keep if c in df.columns]]


def manual_fallback_instructions(tf_name: str, out_path: str) -> None:
    """Print step-by-step MT5 manual CSV export instructions."""
    print(f"""
  ══════════════════════════════════════════════════════
  MANUAL EXPORT NEEDED FOR {tf_name}
  ══════════════════════════════════════════════════════
  All automatic methods failed. Export manually from MT5:

  1. In MT5, open a {tf_name} chart for XAUUSD
  2. Scroll chart all the way LEFT (load full history)
  3. Click:  View → Symbols
  4. Find XAUUSD → right-click → Specification
     (this forces history sync)
  5. Close Specification window
  6. Now click: File → Save As  (while the {tf_name} chart is active)
  7. Save as CSV to:
       {out_path}
  8. Re-run: python data_engine.py

  OR: In MT5 → Tools → History Center → XAUUSD → {tf_name}
      → Download → then Export as CSV to the path above
  ══════════════════════════════════════════════════════
""")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def download_one(tf_name: str, tf_const: int) -> pd.DataFrame | None:
    """Try all 3 methods in order. Return DataFrame or None."""
    print(f"\nDownloading {SYMBOL} {tf_name}...")

    for method in [method1_from_pos, method2_from_date, method3_range_chunks]:
        df = method(tf_const, tf_name)
        if df is not None and len(df) > 100:
            print(f"  SUCCESS: {len(df):,} candles  "
                  f"({df.index.min().date()} → {df.index.max().date()})")
            return df
        time.sleep(1)

    return None


def run() -> None:
    print("\n" + "=" * 55)
    print("  CopetraNova — data_engine.py  (bulletproof)")
    print("=" * 55 + "\n")

    if not connect_mt5():
        sys.exit(1)

    if not ensure_symbol_visible():
        mt5.shutdown()
        sys.exit(1)

    results   = {}
    all_ok    = True

    for tf_name, (tf_const, _) in TIMEFRAMES.items():
        out_path = os.path.join(RAW_DIR, f"{SYMBOL}_{tf_name}.csv")

        # skip if already downloaded and has data
        if os.path.exists(out_path):
            existing = pd.read_csv(out_path, index_col=0)
            if len(existing) > 1000:
                print(f"\n  {tf_name}: already exists ({len(existing):,} rows) — skipping.")
                results[tf_name] = len(existing)
                continue

        df = download_one(tf_name, tf_const)

        if df is not None:
            df.to_csv(out_path)
            size_kb = os.path.getsize(out_path) / 1024
            print(f"  Saved → {out_path}  ({size_kb:,.0f} KB)")
            results[tf_name] = len(df)
        else:
            all_ok = False
            manual_fallback_instructions(tf_name, out_path)

    mt5.shutdown()

    print("\n" + "=" * 55)
    if all_ok:
        print("  Download complete!")
        for tf_name, count in results.items():
            print(f"  {tf_name:4s}  →  {count:,} candles")
        print(f"\n  Files saved to: {RAW_DIR}")
        print("\n  Next step:  python feature_engine.py\n")
    else:
        print("  Some timeframes failed — see MANUAL EXPORT instructions above.")
        print("  Fix those files, then run: python feature_engine.py\n")


if __name__ == "__main__":
    run()