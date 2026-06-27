"""
CopetraNova -- result_reader.py
=================================
Reads trade execution results written by CopetraNova_EA.mq5
and appends them to data/logs/trade_results.csv

This closes the feedback loop:
  Python generates signal -> MQL5 executes trade -> Python logs result

Run this in a SEPARATE terminal window alongside live_bot.py:
    python result_reader.py

It runs silently and checks for new results every 5 seconds.
Stop with Ctrl+C.
"""

import os
import time
import pandas as pd
from datetime import datetime, timezone

# -- paths ---------------------------------------------------------------------
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(BASE_DIR, "data", "result.txt")
SIGNAL_FILE = os.path.join(BASE_DIR, "data", "signal.txt")
LOG_DIR     = os.path.join(BASE_DIR, "data", "logs")
RESULTS_CSV = os.path.join(LOG_DIR, "trade_results.csv")

os.makedirs(LOG_DIR, exist_ok=True)

# track what we already processed
last_result_time = ""


def read_result_file() -> dict | None:
    """Read and parse the result.txt written by MQL5 EA."""
    if not os.path.exists(RESULT_FILE):
        return None

    result = {}
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    result[key.strip()] = val.strip()
    except Exception as e:
        print(f"  [result_reader] Read error: {e}")
        return None

    return result if result else None


def log_result(result: dict) -> None:
    """Append trade result to trade_results.csv"""
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "logged_at":  now_str,
        "direction":  result.get("DIRECTION", ""),
        "status":     result.get("STATUS", ""),
        "grade":      result.get("GRADE", ""),
        "confidence": result.get("CONFIDENCE", ""),
        "lot":        result.get("LOT", ""),
        "tp_pips":    result.get("TP_PIPS", ""),
        "sl_pips":    result.get("SL_PIPS", ""),
        "ticket":     result.get("TICKET", ""),
        "ea_time":    result.get("TIMESTAMP", ""),
    }
    df_row = pd.DataFrame([row])
    header = not os.path.exists(RESULTS_CSV)
    df_row.to_csv(RESULTS_CSV, mode="a", header=header, index=False)

    status  = result.get("STATUS", "UNKNOWN")
    dir_str = result.get("DIRECTION", "?")
    grade   = result.get("GRADE", "?")
    lot     = result.get("LOT", "?")
    ticket  = result.get("TICKET", "?")

    if status == "EXECUTED":
        print(f"  [TRADE EXECUTED]  {dir_str}  grade={grade}  "
              f"lot={lot}  ticket={ticket}")
    else:
        print(f"  [TRADE FAILED]    {dir_str}  -- check MT5 journal")


def print_summary() -> None:
    """Print running accuracy summary from trade_results.csv"""
    if not os.path.exists(RESULTS_CSV):
        return
    df = pd.read_csv(RESULTS_CSV)
    total     = len(df)
    executed  = len(df[df["status"] == "EXECUTED"])
    failed    = len(df[df["status"] == "FAILED"])
    buys      = len(df[df["direction"] == "BUY"])
    sells     = len(df[df["direction"] == "SELL"])

    print(f"\n  [SUMMARY]  Total trades: {total}  |  "
          f"Executed: {executed}  |  Failed: {failed}  |  "
          f"BUY: {buys}  |  SELL: {sells}")


def run() -> None:
    global last_result_time

    print("\n" + "=" * 54)
    print("  CopetraNova -- result_reader.py")
    print("  Monitoring MQL5 EA trade execution results")
    print("=" * 54)
    print(f"\n  Result file : {RESULT_FILE}")
    print(f"  Results log : {RESULTS_CSV}")
    print(f"\n  Checking every 5 seconds...  (Ctrl+C to stop)\n")

    check_count = 0

    while True:
        try:
            result = read_result_file()

            if result:
                timestamp = result.get("TIMESTAMP", "")

                # only process if this is a new result
                if timestamp != last_result_time:
                    last_result_time = timestamp
                    log_result(result)
                    print_summary()

            check_count += 1
            # print heartbeat every 60 checks (5 minutes)
            if check_count % 60 == 0:
                now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
                signal_exists = os.path.exists(SIGNAL_FILE)
                print(f"  [{now} UTC]  Monitoring...  "
                      f"Signal pending: {signal_exists}")

            time.sleep(5)

        except KeyboardInterrupt:
            print("\n\n[CopetraNova] result_reader stopped.")
            print(f"  Results saved to: {RESULTS_CSV}")
            break

        except Exception as e:
            print(f"  [ERROR] {e}")
            time.sleep(10)


if __name__ == "__main__":
    run()