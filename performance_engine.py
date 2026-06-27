"""
CopetraNova -- performance_engine.py
======================================
Complete trade analytics and performance tracking engine.

PURPOSE:
  Record every trade outcome with full context.
  Analyse performance by tier, session, ADX zone, direction.
  Track equity curve, drawdown, streaks, and expectancy.
  Generate daily and all-time reports.
  Export to CSV for external analysis.

FEATURES:
  A. Trade log with full context     : tier, session, ADX, direction,
                                       confidence score, RR planned vs actual
  B. Winrate by dimension            : tier / session / ADX zone / direction
  C. Expectancy per category         : avg win * winrate - avg loss * lossrate
  D. RR analysis                     : planned RR vs actually achieved RR
  E. Equity curve                    : balance per trade for chart/export
  F. Drawdown tracking               : max drawdown, current drawdown
  G. Streak tracking                 : best win streak, worst loss streak
  H. Daily summary report            : printed and saved to JSON
  I. CSV export                      : all trades exportable for Excel
  J. Best / worst categories         : which tier/session performs best
  K. Persistent storage              : JSON log survives restarts

USAGE in live_bot.py:
  from performance_engine import PerformanceEngine

  perf = PerformanceEngine()

  # After each trade closes:
  perf.record_trade(
      signal_id    = sid,
      direction    = 1,          # 1=BUY, -1=SELL
      tier         = "TREND",    # SCALP / TREND / SWING
      session      = "LONDON",   # ASIA / LONDON / OVERLAP / NY
      adx_value    = 28.5,
      confidence   = 67,
      planned_rr   = 2.0,
      sl_pips      = 15,
      tp_pips      = 30,
      pips_result  = 18.5,       # actual pips (+win / -loss)
      commission   = 0.50,
      swap         = 0.0,
      entry_price  = 3341.20,
      exit_price   = 3359.70,
      lot          = 0.02,
      balance_after= 52.40,
  )

  # Get reports:
  perf.print_daily_report()
  perf.print_full_report()
  summary = perf.get_summary_dict()
"""

import os
import json
import csv
from datetime import datetime, timezone


# ==============================================================================
# CONFIG
# ==============================================================================

ADX_STRONG_THRESHOLD  = 25.0   # ADX >= this = STRONG trend
ADX_WEAK_THRESHOLD    = 18.0   # ADX <= this = WEAK trend
MIN_TRADES_FOR_STATS  = 3      # minimum trades before showing category stats

LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "trade_log.json"
)
CSV_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "trade_log.csv"
)
REPORT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "daily_report.json"
)


# ==============================================================================
# PERFORMANCE ENGINE
# ==============================================================================

class PerformanceEngine:
    """
    Complete analytics engine.
    Instantiate ONCE in live_bot.py alongside RiskEngine.
    """

    def __init__(self):
        self.trades     = []    # list of trade dicts
        self.peak_bal   = 0.0
        self.max_dd     = 0.0
        self._load_log()
        self._log("PerformanceEngine ready -- "
                  f"{len(self.trades)} trades loaded")

    # ==========================================================================
    # RECORD TRADE
    # ==========================================================================

    def record_trade(self,
                     signal_id: str,
                     direction: int,
                     tier: str,
                     session: str,
                     adx_value: float,
                     confidence: float,
                     planned_rr: float,
                     sl_pips: int,
                     tp_pips: int,
                     pips_result: float,
                     commission: float = 0.0,
                     swap: float = 0.0,
                     entry_price: float = 0.0,
                     exit_price: float = 0.0,
                     lot: float = 0.01,
                     balance_after: float = 0.0) -> None:
        """
        Record a completed trade with full context.
        Call from live_bot.py after every trade closes.
        """
        outcome     = "WIN" if pips_result > 0 else ("LOSS" if pips_result < 0 else "BE")
        adx_zone    = self._adx_zone(adx_value)
        dir_name    = "BUY" if direction == 1 else "SELL"

        # Actual RR achieved
        actual_rr   = round(abs(pips_result) / sl_pips, 2) if sl_pips > 0 else 0.0
        rr_efficiency = round(actual_rr / planned_rr, 2) if planned_rr > 0 else 0.0

        trade = {
            "signal_id":    signal_id,
            "time":         datetime.now(tz=timezone.utc).isoformat(),
            "direction":    dir_name,
            "tier":         tier,
            "session":      session,
            "adx_value":    round(adx_value, 1),
            "adx_zone":     adx_zone,
            "confidence":   round(confidence, 1),
            "planned_rr":   planned_rr,
            "actual_rr":    actual_rr,
            "rr_efficiency":rr_efficiency,
            "sl_pips":      sl_pips,
            "tp_pips":      tp_pips,
            "pips_result":  round(pips_result, 1),
            "commission":   round(commission, 2),
            "swap":         round(swap, 2),
            "entry_price":  round(entry_price, 2),
            "exit_price":   round(exit_price, 2),
            "lot":          lot,
            "balance_after":round(balance_after, 2),
            "outcome":      outcome,
        }

        self.trades.append(trade)
        self._update_drawdown(balance_after)
        self._save_log()
        self._append_csv(trade)

        self._log(
            f"{outcome}: {tier} {dir_name} {session} | "
            f"{pips_result:+.1f}pips | RR {actual_rr:.1f}R | "
            f"conf={confidence:.0f} | ADX={adx_zone}"
        )

    # ==========================================================================
    # ADX ZONE CLASSIFICATION
    # ==========================================================================

    def _adx_zone(self, adx_value: float) -> str:
        if adx_value >= ADX_STRONG_THRESHOLD:
            return "STRONG"
        elif adx_value <= ADX_WEAK_THRESHOLD:
            return "WEAK"
        return "MODERATE"

    # ==========================================================================
    # DRAWDOWN TRACKING
    # ==========================================================================

    def _update_drawdown(self, balance: float) -> None:
        if balance > self.peak_bal:
            self.peak_bal = balance
        if self.peak_bal > 0:
            dd = (self.peak_bal - balance) / self.peak_bal * 100.0
            if dd > self.max_dd:
                self.max_dd = dd

    # ==========================================================================
    # CORE STATS HELPERS
    # ==========================================================================

    def _stats(self, trades: list) -> dict:
        """Compute stats for any list of trade dicts."""
        if not trades:
            return {
                "count": 0, "wins": 0, "losses": 0,
                "winrate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "expectancy": 0.0, "total_pips": 0.0,
                "avg_rr": 0.0, "best": 0.0, "worst": 0.0,
            }

        wins   = [t for t in trades if t["outcome"] == "WIN"]
        losses = [t for t in trades if t["outcome"] == "LOSS"]
        n      = len(trades)
        nw     = len(wins)
        nl     = len(losses)

        winrate  = nw / n if n > 0 else 0.0
        avg_win  = sum(t["pips_result"] for t in wins)  / nw if nw > 0 else 0.0
        avg_loss = sum(t["pips_result"] for t in losses) / nl if nl > 0 else 0.0
        expect   = (winrate * avg_win) + ((1 - winrate) * avg_loss)
        avg_rr   = sum(t["actual_rr"] for t in trades) / n
        total    = sum(t["pips_result"] for t in trades)
        best     = max((t["pips_result"] for t in trades), default=0.0)
        worst    = min((t["pips_result"] for t in trades), default=0.0)

        return {
            "count":      n,
            "wins":       nw,
            "losses":     nl,
            "winrate":    round(winrate * 100, 1),
            "avg_win":    round(avg_win, 1),
            "avg_loss":   round(avg_loss, 1),
            "expectancy": round(expect, 2),
            "total_pips": round(total, 1),
            "avg_rr":     round(avg_rr, 2),
            "best":       round(best, 1),
            "worst":      round(worst, 1),
        }

    def _filter(self, **kwargs) -> list:
        """Filter trades by any field value."""
        result = self.trades
        for key, val in kwargs.items():
            result = [t for t in result if t.get(key) == val]
        return result

    # ==========================================================================
    # STREAK TRACKING
    # ==========================================================================

    def _streaks(self) -> dict:
        if not self.trades:
            return {"best_win_streak": 0, "worst_loss_streak": 0,
                    "current_streak": 0, "current_streak_type": "NONE"}
        best_win  = 0
        worst_los = 0
        cur       = 0
        cur_type  = self.trades[-1]["outcome"]

        for t in reversed(self.trades):
            if t["outcome"] == cur_type:
                cur += 1
            else:
                break

        run_w = 0
        run_l = 0
        max_w = 0
        max_l = 0
        for t in self.trades:
            if t["outcome"] == "WIN":
                run_w += 1; run_l = 0
            elif t["outcome"] == "LOSS":
                run_l += 1; run_w = 0
            else:
                run_w = 0; run_l = 0
            max_w = max(max_w, run_w)
            max_l = max(max_l, run_l)

        return {
            "best_win_streak":   max_w,
            "worst_loss_streak": max_l,
            "current_streak":    cur,
            "current_streak_type": cur_type,
        }

    # ==========================================================================
    # TODAY'S TRADES
    # ==========================================================================

    def _today_trades(self) -> list:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return [t for t in self.trades if t["time"].startswith(today)]

    # ==========================================================================
    # BEST / WORST CATEGORY
    # ==========================================================================

    def _best_category(self, dimension: str) -> tuple:
        """Return (best_value, expectancy) for a dimension field."""
        values = set(t.get(dimension, "") for t in self.trades)
        best   = None
        best_e = -9999
        for v in values:
            s = self._stats([t for t in self.trades if t.get(dimension) == v])
            if s["count"] >= MIN_TRADES_FOR_STATS and s["expectancy"] > best_e:
                best_e = s["expectancy"]
                best   = v
        return best, best_e

    # ==========================================================================
    # FULL SUMMARY DICT
    # ==========================================================================

    def get_summary_dict(self) -> dict:
        """Full stats dict -- used by live_bot.py and report generation."""
        overall    = self._stats(self.trades)
        today_t    = self._today_trades()
        today_st   = self._stats(today_t)
        streaks    = self._streaks()

        by_tier    = {tier: self._stats(self._filter(tier=tier))
                      for tier in ["SCALP", "TREND", "SWING"]}
        by_session = {s: self._stats(self._filter(session=s))
                      for s in ["ASIA", "LONDON", "OVERLAP", "NY"]}
        by_adx     = {z: self._stats(self._filter(adx_zone=z))
                      for z in ["STRONG", "MODERATE", "WEAK"]}
        by_dir     = {d: self._stats(self._filter(direction=d))
                      for d in ["BUY", "SELL"]}

        best_tier,    best_tier_e    = self._best_category("tier")
        best_session, best_session_e = self._best_category("session")
        best_adx,     best_adx_e     = self._best_category("adx_zone")

        # RR planned vs achieved
        rr_trades  = [t for t in self.trades if t["planned_rr"] > 0]
        avg_plan   = (sum(t["planned_rr"] for t in rr_trades) /
                      len(rr_trades)) if rr_trades else 0.0
        avg_actual = (sum(t["actual_rr"] for t in rr_trades) /
                      len(rr_trades)) if rr_trades else 0.0

        return {
            "overall":         overall,
            "today":           today_st,
            "by_tier":         by_tier,
            "by_session":      by_session,
            "by_adx_zone":     by_adx,
            "by_direction":    by_dir,
            "streaks":         streaks,
            "max_drawdown_pct":round(self.max_dd, 2),
            "peak_balance":    round(self.peak_bal, 2),
            "avg_planned_rr":  round(avg_plan, 2),
            "avg_actual_rr":   round(avg_actual, 2),
            "rr_efficiency":   round(avg_actual / avg_plan, 2) if avg_plan > 0 else 0,
            "best_tier":       {"name": best_tier, "expectancy": round(best_tier_e, 2)},
            "best_session":    {"name": best_session, "expectancy": round(best_session_e, 2)},
            "best_adx_zone":   {"name": best_adx,  "expectancy": round(best_adx_e, 2)},
        }

    # ==========================================================================
    # DAILY REPORT -- printed to terminal
    # ==========================================================================

    def print_daily_report(self) -> None:
        today_t = self._today_trades()
        st      = self._stats(today_t)
        today   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        print()
        print(f"  ===== DAILY REPORT {today} =====")
        print(f"  Trades  : {st['count']}  "
              f"W:{st['wins']}  L:{st['losses']}  "
              f"WR:{st['winrate']}%")
        print(f"  PnL     : {st['total_pips']:+.1f} pips")
        print(f"  Expect  : {st['expectancy']:+.2f} pips/trade")
        print(f"  Avg RR  : {st['avg_rr']:.2f}R")
        print(f"  Best    : {st['best']:+.1f}pips  "
              f"Worst: {st['worst']:+.1f}pips")

        if today_t:
            print()
            print("  By Tier:")
            for tier in ["SCALP", "TREND", "SWING"]:
                t_trades = [t for t in today_t if t["tier"] == tier]
                if t_trades:
                    s = self._stats(t_trades)
                    print(f"    {tier:7s}: {s['count']}T "
                          f"{s['winrate']}%WR "
                          f"{s['total_pips']:+.1f}pips")

            print()
            print("  By Session:")
            for sess in ["ASIA", "LONDON", "OVERLAP", "NY"]:
                s_trades = [t for t in today_t if t["session"] == sess]
                if s_trades:
                    s = self._stats(s_trades)
                    print(f"    {sess:8s}: {s['count']}T "
                          f"{s['winrate']}%WR "
                          f"{s['total_pips']:+.1f}pips")

        print(f"  ===================================")
        print()

        # Save daily report to JSON
        self._save_daily_report(today, st, today_t)

    # ==========================================================================
    # FULL REPORT -- all-time stats
    # ==========================================================================

    def print_full_report(self) -> None:
        s      = self.get_summary_dict()
        ov     = s["overall"]
        stk    = s["streaks"]

        print()
        print("  ===== ALL-TIME PERFORMANCE REPORT =====")
        print(f"  Trades    : {ov['count']}  "
              f"W:{ov['wins']}  L:{ov['losses']}  "
              f"WR:{ov['winrate']}%")
        print(f"  Total PnL : {ov['total_pips']:+.1f} pips")
        print(f"  Expectancy: {ov['expectancy']:+.2f} pips/trade")
        print(f"  Avg Win   : {ov['avg_win']:+.1f}  "
              f"Avg Loss: {ov['avg_loss']:+.1f}")
        print(f"  Avg RR    : {ov['avg_rr']:.2f}R  "
              f"Planned: {s['avg_planned_rr']:.2f}R  "
              f"Actual: {s['avg_actual_rr']:.2f}R  "
              f"Eff: {s['rr_efficiency']:.0%}")
        print(f"  Max DD    : {s['max_drawdown_pct']:.2f}%  "
              f"Peak Bal: ${s['peak_balance']:.2f}")
        print(f"  Win streak: {stk['best_win_streak']}  "
              f"Loss streak: {stk['worst_loss_streak']}  "
              f"Current: {stk['current_streak']} {stk['current_streak_type']}")

        print()
        print("  By Tier:")
        for tier in ["SCALP", "TREND", "SWING"]:
            t = s["by_tier"][tier]
            if t["count"] >= 1:
                print(f"    {tier:7s}: {t['count']:3d}T  "
                      f"WR:{t['winrate']:5.1f}%  "
                      f"Exp:{t['expectancy']:+.2f}  "
                      f"PnL:{t['total_pips']:+.1f}pips")

        print()
        print("  By Session:")
        for sess in ["ASIA", "LONDON", "OVERLAP", "NY"]:
            t = s["by_session"][sess]
            if t["count"] >= 1:
                print(f"    {sess:8s}: {t['count']:3d}T  "
                      f"WR:{t['winrate']:5.1f}%  "
                      f"Exp:{t['expectancy']:+.2f}  "
                      f"PnL:{t['total_pips']:+.1f}pips")

        print()
        print("  By ADX Zone:")
        for zone in ["STRONG", "MODERATE", "WEAK"]:
            t = s["by_adx_zone"][zone]
            if t["count"] >= 1:
                print(f"    {zone:8s}: {t['count']:3d}T  "
                      f"WR:{t['winrate']:5.1f}%  "
                      f"Exp:{t['expectancy']:+.2f}  "
                      f"PnL:{t['total_pips']:+.1f}pips")

        print()
        print("  By Direction:")
        for d in ["BUY", "SELL"]:
            t = s["by_direction"][d]
            if t["count"] >= 1:
                print(f"    {d:5s}: {t['count']:3d}T  "
                      f"WR:{t['winrate']:5.1f}%  "
                      f"Exp:{t['expectancy']:+.2f}  "
                      f"PnL:{t['total_pips']:+.1f}pips")

        print()
        if s["best_tier"]["name"]:
            print(f"  Best Tier   : {s['best_tier']['name']} "
                  f"(exp={s['best_tier']['expectancy']:+.2f})")
        if s["best_session"]["name"]:
            print(f"  Best Session: {s['best_session']['name']} "
                  f"(exp={s['best_session']['expectancy']:+.2f})")
        baz = s.get("best_adx_zone", {})
        if baz.get("name"):
                print(f"  Best ADZ    : {baz['name']} "
                      f"(exp={baz.get('expectancy', 0):+.2f})")

        print("  ========================================")
        print()

    # ==========================================================================
    # ONE-LINE STATUS for live_bot.py terminal
    # ==========================================================================

    def get_status_line(self) -> str:
        today_t = self._today_trades()
        st      = self._stats(today_t)
        ov      = self._stats(self.trades)
        stk     = self._streaks()
        return (
            f"Today: {st['count']}T "
            f"WR:{st['winrate']}% "
            f"PnL:{st['total_pips']:+.1f}pips | "
            f"All-time: {ov['count']}T "
            f"WR:{ov['winrate']}% "
            f"Exp:{ov['expectancy']:+.2f} | "
            f"Streak: {stk['current_streak']} {stk['current_streak_type']} | "
            f"MaxDD: {self.max_dd:.1f}%"
        )

    # ==========================================================================
    # EQUITY CURVE -- list of (time, balance) tuples
    # ==========================================================================

    def get_equity_curve(self) -> list:
        return [(t["time"][:16], t["balance_after"])
                for t in self.trades if t["balance_after"] > 0]

    # ==========================================================================
    # PERSISTENCE
    # ==========================================================================

    def _save_log(self) -> None:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        tmp = LOG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "trades":   self.trades,
                    "peak_bal": self.peak_bal,
                    "max_dd":   self.max_dd,
                    "saved_at": datetime.now(tz=timezone.utc).isoformat(),
                }, f, indent=2)
            os.replace(tmp, LOG_FILE)
        except Exception as e:
            self._log(f"Log save error: {e}")
            try:
                os.remove(tmp)
            except Exception:
                pass

    def _load_log(self) -> None:
        if not os.path.exists(LOG_FILE):
            self.trades   = []
            self.peak_bal = 0.0
            self.max_dd   = 0.0
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.trades   = data.get("trades",   [])
            self.peak_bal = data.get("peak_bal", 0.0)
            self.max_dd   = data.get("max_dd",   0.0)
        except Exception as e:
            self._log(f"Log load error: {e} -- starting fresh")
            self.trades   = []
            self.peak_bal = 0.0
            self.max_dd   = 0.0

    def _save_daily_report(self, today: str,
                            stats: dict, today_trades: list) -> None:
        os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
        report = {
            "date":         today,
            "stats":        stats,
            "trade_count":  len(today_trades),
            "trades":       today_trades,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        tmp = REPORT_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            os.replace(tmp, REPORT_FILE)
        except Exception as e:
            self._log(f"Daily report save error: {e}")

    # ==========================================================================
    # CSV EXPORT
    # ==========================================================================

    def _append_csv(self, trade: dict) -> None:
        """Append one trade row to CSV. Creates header if new file."""
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
        file_exists = os.path.exists(CSV_FILE)
        fields = [
            "signal_id", "time", "outcome", "direction", "tier",
            "session", "adx_zone", "adx_value", "confidence",
            "planned_rr", "actual_rr", "rr_efficiency",
            "sl_pips", "tp_pips", "pips_result",
            "commission", "swap", "entry_price", "exit_price",
            "lot", "balance_after",
        ]
        try:
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields,
                                        extrasaction="ignore")
                if not file_exists:
                    writer.writeheader()
                writer.writerow(trade)
        except Exception as e:
            self._log(f"CSV write error: {e}")

    def export_csv(self, path: str = None) -> str:
        """Re-export entire trade log to CSV. Returns file path."""
        out = path or CSV_FILE
        os.makedirs(os.path.dirname(out), exist_ok=True)
        fields = [
            "signal_id", "time", "outcome", "direction", "tier",
            "session", "adx_zone", "adx_value", "confidence",
            "planned_rr", "actual_rr", "rr_efficiency",
            "sl_pips", "tp_pips", "pips_result",
            "commission", "swap", "entry_price", "exit_price",
            "lot", "balance_after",
        ]
        try:
            with open(out, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self.trades)
            self._log(f"CSV exported: {out} ({len(self.trades)} trades)")
            return out
        except Exception as e:
            self._log(f"CSV export error: {e}")
            return ""

    # ==========================================================================
    # RESET
    # ==========================================================================

    def reset_all(self) -> None:
        """Hard reset -- wipes all trade history. Use with caution."""
        self.trades   = []
        self.peak_bal = 0.0
        self.max_dd   = 0.0
        for f in [LOG_FILE, CSV_FILE, REPORT_FILE]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        self._log("All trade history reset")

    # ==========================================================================
    # HELPER
    # ==========================================================================

    def _log(self, msg: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        print(f"  [PerfEngine {ts}] {msg}")