"""
CopetraNova -- risk_engine.py  (Production Final)
===================================================
Complete risk management engine for XAUUSD live trading.

ALL 10 FINAL ISSUES FIXED:

  FIX 1  : sync_account() now calls _check_day_reset(equity).
            Daily limits reset at UTC midnight even in continuous runs.

  FIX 2  : Commission sign handling corrected.
            net_pnl = gross_pnl + commission + swap
            MT5 returns commission as negative, swap as +/-.
            Adding both directly is correct and safe.

  FIX 3  : record_result() no longer mutates account_balance locally.
            MT5 is sole source of truth via sync_account().
            Prevents temporary double-accounting between candle loops.

  FIX 4  : Floating PnL reset after last trade closes.
            When open_trades is empty after pop(), floating_pnl = 0.0.
            Prevents stale floating PnL from showing after close.

  FIX 5  : reconcile_positions() added.
            Compares internal open_trades to live MT5 position IDs.
            Removes stale internal entries caused by terminal restart,
            VPS crash, manual close, or broker rejection.
            Call every candle from live_bot.py.

  FIX 6  : Trade age timezone safety.
            open_dt forced to UTC if tzinfo is None before subtraction.
            Prevents silent crash from aware/naive datetime mismatch.

  FIX 7  : Daily hard stop includes floating losses.
            effective_daily_loss = realized + floating (negative portion).
            Prevents opening new trades while sitting on large float loss.

  FIX 8  : update_floating_pnl() documented clearly.
            Broker-reported profit may include commission/swap.
            Behaviour documented so debugging is not painful later.

  FIX 9  : threading.Lock() added around save/load operations.
            Prevents state corruption if multiple threads write simultaneously.
            Safe for single-thread bots, mandatory for multi-thread systems.

  FIX 10 : Risk budget comment added about correlation limitation.
            Current implementation treats all symbols equally.
            Correlation bucketing is next institutional upgrade.

ALL PREVIOUS FIXES RETAINED:
  - Stored lot in register_trade and record_result
  - Peak equity drawdown tracking
  - Atomic JSON save with os.replace()
  - Stored lot in exposure calculation
  - Independent partial TP + trailing (not elif)
  - Dynamic slippage from ATR
  - Soft stop 0.7x lot reduction only (not double penalty)
  - pip_size configurable at init
  - trail_sl None-safe comparison
  - pop() safe everywhere
  - outcome auto-derived from pips sign
  - commission + swap in net PnL
  - Trail distance capped at MAX_TRAIL_PIPS
  - Same-direction correlation limit
  - floating_pnl properly tracked
  - Available risk budget with floating separation

USAGE in live_bot.py:
  from risk_engine import RiskEngine, compute_pip_value

  pip_val = compute_pip_value(mt5.symbol_info(SYMBOL))
  risk    = RiskEngine(account_balance=acct.balance,
                       pip_value_per_lot=pip_val)

  # Each candle:
  acct = mt5.account_info()
  risk.sync_account(acct.balance, acct.equity)

  positions = mt5.positions_get(symbol=SYMBOL) or []
  risk.reconcile_positions({str(p.ticket) for p in positions})

  pos_dict = {str(p.ticket): {"profit": p.profit} for p in positions}
  risk.update_floating_pnl(pos_dict)

  allowed, reason = risk.check_risk(
      equity=acct.equity,
      spread_pips=spread,
      atr_value=feat["_atr_value"],
      atr_pct=feat["_atr_pct"],
      direction=direction
  )
"""

import os
import json
import math
import threading
from datetime import datetime, timezone, timedelta


# ==============================================================================
# CONFIG
# ==============================================================================

DEFAULT_BALANCE       = 50.0
DEFAULT_PIP_SIZE      = 0.1
RISK_PER_TRADE_PCT    = 0.01
DAILY_SOFT_STOP_PCT   = 0.02
DAILY_HARD_STOP_PCT   = 0.03
MAX_DRAWDOWN_PCT      = 0.10
PIP_VALUE_DEFAULT     = 10.0
MIN_LOT               = 0.01
MAX_LOT               = 1.00
EXPECTED_SLIPPAGE     = 1.0

ATR_RISK_TABLE = [
    (80, 0.40),
    (65, 0.60),
    (45, 0.80),
    (0,  1.00),
]

CONSEC_LOSS_LIMIT     = 4
COOLDOWN_HOURS        = 2
POST_LOSS_PAUSE_MIN   = 15

BREAKEVEN_AT_R        = 1.0
BREAKEVEN_BUFFER      = 2.0
PARTIAL_TP_AT_R       = 1.5
TRAIL_ATR_MULT        = 1.2
MAX_TRAIL_PIPS        = 40.0

MAX_TRADE_AGE = {
    "SCALP": 45,
    "TREND": 240,
    "SWING": 480,
}

MAX_OPEN_TRADES       = 3
MAX_TOTAL_RISK_PCT    = 3.0
MAX_SAME_DIRECTION    = 2

MAX_SPREAD_ATR_RATIO  = 0.20

CONF_PENALTY_PER_LOSS = 2.0
MAX_CONF_PENALTY      = 10.0

SOFT_STOP_LOT_MULT    = 0.7
EQUITY_HISTORY_DAYS   = 90

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "risk_state.json"
)


# ==============================================================================
# RISK ENGINE
# ==============================================================================

class RiskEngine:
    """
    Production risk engine.
    Instantiate ONCE in live_bot.py at startup.
    Call sync_account() and reconcile_positions() every candle.
    """

    def __init__(self,
                 account_balance: float = DEFAULT_BALANCE,
                 pip_value_per_lot: float = PIP_VALUE_DEFAULT,
                 pip_size: float = DEFAULT_PIP_SIZE):

        self.pip_value_per_lot = pip_value_per_lot
        self.pip_size          = pip_size
        self.starting_balance  = account_balance
        self._lock             = threading.Lock()   # FIX 9

        loaded = self._load_state()

        if not loaded:
            self.account_balance    = account_balance
            self.peak_equity        = account_balance
            self.day_start_balance  = account_balance
            self.daily_pnl_pips     = 0.0
            self.daily_pnl_dollars  = 0.0
            self.floating_pnl       = 0.0
            self.trades_today       = 0
            self.day_date           = self._today()
            self.consecutive_losses = 0
            self.total_wins         = 0
            self.total_losses       = 0
            self.cooldown_until     = None
            self.pause_until        = None
            self.open_trades        = {}
            self.equity_history     = []

        self._log(
            f"Started | Balance: ${self.account_balance:.2f} | "
            f"Peak: ${self.peak_equity:.2f} | "
            f"pip_value: ${pip_value_per_lot:.2f}/lot"
        )

    # ==========================================================================
    # FIX 1: sync_account triggers daily reset
    # ==========================================================================

    def sync_account(self, balance: float, equity: float) -> None:
        """
        FIX 1: Sync from MT5 every candle. MT5 is sole source of truth.
        Also calls _check_day_reset() to handle UTC midnight correctly.
        """
        self.account_balance = balance
        self._update_peak(equity)
        self._check_day_reset(equity)   # FIX 1: was missing

    # ==========================================================================
    # DAILY RESET
    # ==========================================================================

    def _today(self) -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _check_day_reset(self, equity: float = None) -> None:
        today = self._today()
        if today != self.day_date:
            eq = equity if equity else self.account_balance
            self.equity_history.append((self.day_date, round(eq, 2)))
            if len(self.equity_history) > EQUITY_HISTORY_DAYS:
                self.equity_history = self.equity_history[-EQUITY_HISTORY_DAYS:]
            self.daily_pnl_pips    = 0.0
            self.daily_pnl_dollars = 0.0
            self.floating_pnl      = 0.0
            self.trades_today      = 0
            self.day_date          = today
            self.day_start_balance = eq
            self._log(f"New day reset. Day start: ${eq:.2f}")
            self._save_state()

    def _update_peak(self, equity: float) -> None:
        if equity > self.peak_equity:
            self.peak_equity = equity

    # ==========================================================================
    # FIX 5: RECONCILE POSITIONS WITH MT5
    # ==========================================================================

    def reconcile_positions(self, mt5_position_ids: set) -> None:
        """
        FIX 5: Remove internal open_trades entries that no longer
        exist in MT5 (closed manually, rejected, VPS crash etc).

        Call every candle from live_bot.py:
          positions = mt5.positions_get(symbol=SYMBOL) or []
          risk.reconcile_positions({str(p.ticket) for p in positions})
        """
        local_ids = set(self.open_trades.keys())
        stale     = local_ids - mt5_position_ids

        if stale:
            for sid in stale:
                self._log(f"Reconcile: removing stale trade {sid}")
                self.open_trades.pop(sid, None)

            # FIX 4: reset floating if no more open trades
            if not self.open_trades:
                self.floating_pnl = 0.0

            self._save_state()

    # ==========================================================================
    # FIX 8: FLOATING PNL -- documented clearly
    # ==========================================================================

    def update_floating_pnl(self, open_positions: dict) -> None:
        """
        FIX 8: Uses broker-reported floating profit directly.
        MT5 profit field may already include commission and swap
        depending on broker configuration.
        Document this clearly in your broker setup notes.

        Call every candle from live_bot.py:
          positions = mt5.positions_get(symbol=SYMBOL) or []
          pos_dict  = {str(p.ticket): {"profit": p.profit}
                       for p in positions}
          risk.update_floating_pnl(pos_dict)
        """
        total = sum(float(p.get("profit", 0.0)) for p in open_positions.values())
        self.floating_pnl = round(total, 2)

    # ==========================================================================
    # FIX 7: MAIN RISK CHECK -- includes floating in daily stop
    # ==========================================================================

    def check_risk(self,
                   equity: float = None,
                   spread_pips: float = 0.0,
                   atr_value: float = 0.0,
                   atr_pct: float = 50.0,
                   direction: int = 0) -> tuple:
        """
        FIX 7: Daily stop now includes floating losses.
        effective_daily_loss = realized loss + floating loss.
        Prevents opening new trades while sitting on large unrealized loss.
        """
        eq = equity if equity else self.account_balance
        now = datetime.now(tz=timezone.utc)

        # Peak drawdown
        peak_dd = (self.peak_equity - eq) / self.peak_equity if self.peak_equity > 0 else 0
        if peak_dd >= MAX_DRAWDOWN_PCT:
            return False, (f"EMERGENCY STOP: peak drawdown "
                           f"{peak_dd*100:.1f}% >= {MAX_DRAWDOWN_PCT*100:.0f}%")

        # FIX 7: daily stop includes floating losses
        realized_loss = abs(min(0.0, self.daily_pnl_dollars))
        floating_loss = abs(min(0.0, self.floating_pnl))
        effective_daily_loss = realized_loss + floating_loss

        if self.day_start_balance > 0:
            effective_loss_pct = effective_daily_loss / self.day_start_balance
        else:
            effective_loss_pct = 0.0

        if effective_loss_pct >= DAILY_HARD_STOP_PCT:
            return False, (
                f"Daily hard stop: realized=${realized_loss:.2f} + "
                f"floating=${floating_loss:.2f} = "
                f"${effective_daily_loss:.2f} "
                f"({effective_loss_pct*100:.1f}% of day start)"
            )

        # Consecutive loss cooldown
        if self.cooldown_until:
            cd = self._parse_dt(self.cooldown_until)
            if cd and now < cd:
                mins = int((cd - now).total_seconds() / 60)
                return False, f"Consecutive loss cooldown: {mins}min remaining"

        # Post-loss pause
        if self.pause_until:
            p = self._parse_dt(self.pause_until)
            if p and now < p:
                mins = int((p - now).total_seconds() / 60)
                return False, f"Post-loss pause: {mins}min remaining"

        # Max open trades
        if len(self.open_trades) >= MAX_OPEN_TRADES:
            return False, f"Max open trades ({len(self.open_trades)}/{MAX_OPEN_TRADES})"

        # Same-direction correlation limit
        if direction != 0:
            same_dir = sum(
                1 for t in self.open_trades.values()
                if t.get("direction") == direction
            )
            if same_dir >= MAX_SAME_DIRECTION:
                dir_name = "BUY" if direction == 1 else "SELL"
                return False, (f"Correlation limit: {same_dir} open "
                               f"{dir_name} >= {MAX_SAME_DIRECTION}")

        # Total concurrent risk
        total_risk = self._get_total_open_risk(eq)
        if total_risk >= MAX_TOTAL_RISK_PCT:
            return False, f"Max concurrent risk ({total_risk:.1f}% >= {MAX_TOTAL_RISK_PCT}%)"

        # Spread quality
        if atr_value > 0 and spread_pips > 0:
            spread_ratio = (spread_pips * self.pip_size) / atr_value
            if spread_ratio > MAX_SPREAD_ATR_RATIO:
                return False, (f"Spread too wide: ratio={spread_ratio:.2f}")

        # Soft stop (allowed, reduced size)
        if effective_loss_pct >= DAILY_SOFT_STOP_PCT:
            return True, f"SOFT_STOP_ACTIVE: effective_loss={effective_loss_pct*100:.1f}%"

        return True, ""

    # ==========================================================================
    # DYNAMIC LOT SIZING
    # ==========================================================================

    def get_lot_size(self,
                     sl_pips: int,
                     equity: float = None,
                     atr_pct: float = 50.0,
                     atr_value: float = 0.0) -> float:
        if sl_pips <= 0:
            return MIN_LOT

        eq            = equity if equity else self.account_balance
        risk_pct      = self._get_adaptive_risk_pct(atr_pct)

        if self.is_soft_stop_active():
            risk_pct *= SOFT_STOP_LOT_MULT

        risk_pct     *= self.get_equity_curve_factor(eq)
        risk_dollars  = eq * risk_pct

        dynamic_slip  = max(EXPECTED_SLIPPAGE, atr_value * 0.1) if atr_value > 0 \
                        else EXPECTED_SLIPPAGE
        effective_sl  = sl_pips + dynamic_slip

        lot = risk_dollars / (effective_sl * self.pip_value_per_lot)
        return self._normalise_lot(lot)

    def _get_adaptive_risk_pct(self, atr_pct: float) -> float:
        for threshold, factor in ATR_RISK_TABLE:
            if atr_pct >= threshold:
                return RISK_PER_TRADE_PCT * factor
        return RISK_PER_TRADE_PCT

    def _normalise_lot(self, lot: float) -> float:
        lot = math.floor(lot * 100) / 100.0
        return max(MIN_LOT, min(MAX_LOT, lot))

    # ==========================================================================
    # TRADE REGISTRATION
    # ==========================================================================

    def register_trade(self,
                       signal_id: str,
                       direction: int,
                       entry: float,
                       sl_pips: int,
                       tp_pips: int,
                       tier: str = "TREND",
                       lot: float = 0.01) -> None:
        self.open_trades[signal_id] = {
            "direction":      direction,
            "entry":          entry,
            "sl_pips":        sl_pips,
            "tp_pips":        tp_pips,
            "tier":           tier,
            "lot":            lot,
            "breakeven_done": False,
            "partial_done":   False,
            "trail_sl":       None,
            "open_time":      datetime.now(tz=timezone.utc).isoformat(),
        }
        self.trades_today += 1
        self._save_state()
        self._log(
            f"Registered: {signal_id} | "
            f"{'BUY' if direction == 1 else 'SELL'} | "
            f"lot={lot:.2f} | tier={tier} | entry={entry:.2f}"
        )

    # ==========================================================================
    # TRADE MANAGEMENT
    # ==========================================================================

    def check_trade_management(self,
                                signal_id: str,
                                current_price: float,
                                stable_atr: float,
                                tier: str = "TREND") -> dict:
        """
        FIX 6: Trade age calculation forces open_dt to UTC before subtraction.
        FIX 4 prev: independent if blocks for breakeven, partial, trail.
        FIX (trail): trail distance capped at MAX_TRAIL_PIPS.
        """
        actions = {
            "breakeven":   False,
            "partial_tp":  False,
            "trail_sl":    0.0,
            "force_close": False,
            "reason":      "",
        }

        if signal_id not in self.open_trades:
            return actions

        trade     = self.open_trades[signal_id]
        entry     = trade["entry"]
        direction = trade["direction"]
        sl_pips   = trade["sl_pips"]
        tier_used = trade.get("tier", tier)
        reasons   = []

        if direction == 1:
            profit_pips = (current_price - entry) / self.pip_size
        else:
            profit_pips = (entry - current_price) / self.pip_size

        one_r = sl_pips

        # Breakeven
        if profit_pips >= one_r * BREAKEVEN_AT_R and not trade["breakeven_done"]:
            trade["breakeven_done"] = True
            buf   = BREAKEVEN_BUFFER * self.pip_size
            be_sl = round(entry + buf if direction == 1 else entry - buf, 2)
            trade["trail_sl"]    = be_sl
            actions["breakeven"] = True
            actions["trail_sl"]  = be_sl
            reasons.append(f"Breakeven SL->{be_sl:.2f}")
            self._log(f"BREAKEVEN {signal_id} profit={profit_pips:.0f}pips")
            self._save_state()

        # Partial TP -- independent
        if profit_pips >= one_r * PARTIAL_TP_AT_R and not trade["partial_done"]:
            trade["partial_done"]  = True
            actions["partial_tp"]  = True
            reasons.append(f"Partial TP {profit_pips:.0f}pips (1.5R)")
            self._log(f"PARTIAL TP {signal_id} profit={profit_pips:.0f}pips")
            self._save_state()

        # Trailing -- independent, capped
        if trade["breakeven_done"] and stable_atr > 0:
            raw_trail    = stable_atr * TRAIL_ATR_MULT
            capped_trail = min(raw_trail, MAX_TRAIL_PIPS * self.pip_size)
            current_sl   = trade.get("trail_sl")

            if direction == 1:
                new_sl = round(current_price - capped_trail, 2)
                if current_sl is None or new_sl > current_sl:
                    trade["trail_sl"]   = new_sl
                    actions["trail_sl"] = new_sl
                    reasons.append(f"Trail SL->{new_sl:.2f}")
            else:
                new_sl = round(current_price + capped_trail, 2)
                if current_sl is None or new_sl < current_sl:
                    trade["trail_sl"]   = new_sl
                    actions["trail_sl"] = new_sl
                    reasons.append(f"Trail SL->{new_sl:.2f}")

        # Trade age -- FIX 6: timezone-safe
        max_age_min   = MAX_TRADE_AGE.get(tier_used, MAX_TRADE_AGE["TREND"])
        open_time_str = trade.get("open_time", "")
        if open_time_str:
            try:
                open_dt = datetime.fromisoformat(open_time_str)
                # FIX 6: force UTC if naive
                if open_dt.tzinfo is None:
                    open_dt = open_dt.replace(tzinfo=timezone.utc)
                age_mins = (datetime.now(tz=timezone.utc) - open_dt).total_seconds() / 60.0
                if age_mins >= max_age_min:
                    actions["force_close"] = True
                    reasons.append(
                        f"Age {age_mins:.0f}min >= {max_age_min}min ({tier_used})"
                    )
                    self._log(f"FORCE CLOSE {signal_id} age={age_mins:.0f}min")
            except Exception as e:
                self._log(f"Age check error {signal_id}: {e}")

        actions["reason"] = " | ".join(reasons) if reasons else ""
        return actions

    # ==========================================================================
    # CLOSE TRADE
    # ==========================================================================

    def close_trade(self, signal_id: str) -> None:
        self.open_trades.pop(signal_id, None)
        # FIX 4: reset floating if no more open trades
        if not self.open_trades:
            self.floating_pnl = 0.0
        self._save_state()

    # ==========================================================================
    # FIX 2 + FIX 3: RESULT RECORDING
    # ==========================================================================

    def record_result(self,
                      pips: float,
                      signal_id: str = "",
                      equity: float = None,
                      commission: float = 0.0,
                      swap: float = 0.0,
                      outcome: str = "") -> None:
        """
        FIX 2: outcome auto-derived from pips sign. Caller cannot mismatch.
        FIX 2: commission sign -- MT5 returns commission as negative,
               so: net_pnl = gross + commission + swap is correct.
        FIX 3: account_balance NOT mutated locally.
               sync_account() from MT5 is sole source of truth.
        FIX 4: floating_pnl reset if no more open trades after close.
        """
        now = datetime.now(tz=timezone.utc)

        # FIX 2: auto-derive outcome
        if not outcome:
            if pips > 0:
                outcome = "WIN"
            elif pips < 0:
                outcome = "LOSS"
            else:
                outcome = "BREAKEVEN"

        # Use stored lot -- not recalculated
        lot = 0.01
        if signal_id and signal_id in self.open_trades:
            lot = self.open_trades[signal_id].get("lot", 0.01)

        # Safe removal
        self.open_trades.pop(signal_id, None)

        # FIX 4: reset floating if no trades left
        if not self.open_trades:
            self.floating_pnl = 0.0

        gross_pnl = pips * lot * self.pip_value_per_lot
        # FIX 2: MT5 commission is negative, swap is +/-.
        # Adding both directly is correct.
        net_pnl   = gross_pnl + commission + swap

        # Update daily tracking
        self.daily_pnl_pips    += pips
        self.daily_pnl_dollars += net_pnl

        # FIX 3: do NOT mutate account_balance here.
        # sync_account(mt5_balance, mt5_equity) is called next candle.

        if outcome == "WIN":
            self.total_wins         += 1
            self.consecutive_losses  = 0
            self.cooldown_until      = None
            self._log(
                f"WIN: +{pips:.1f}pips | gross=${gross_pnl:+.2f} "
                f"comm=${commission:.2f} swap=${swap:.2f} "
                f"net=${net_pnl:+.2f} | "
                f"daily: {self.daily_pnl_pips:+.1f}pips"
            )

        elif outcome == "LOSS":
            self.total_losses       += 1
            self.consecutive_losses += 1
            self.pause_until = (
                now + timedelta(minutes=POST_LOSS_PAUSE_MIN)
            ).isoformat()

            if self.consecutive_losses >= CONSEC_LOSS_LIMIT:
                self.cooldown_until = (
                    now + timedelta(hours=COOLDOWN_HOURS)
                ).isoformat()
                self._log(
                    f"COOLDOWN: {self.consecutive_losses} consec losses "
                    f"-- {COOLDOWN_HOURS}h pause"
                )

            self._log(
                f"LOSS: {pips:.1f}pips | net=${net_pnl:+.2f} | "
                f"consecutive={self.consecutive_losses}"
            )

        elif outcome == "BREAKEVEN":
            self._log(f"BREAKEVEN: {pips:.1f}pips net=${net_pnl:+.2f}")

        self._save_state()

    # ==========================================================================
    # EXPOSURE
    # ==========================================================================

    def _get_total_open_risk(self, equity: float) -> float:
        """
        FIX 10 note: All open trades treated equally regardless of symbol.
        Future upgrade: symbol exposure bucketing for correlation control.
        """
        if equity <= 0 or not self.open_trades:
            return 0.0
        total = 0.0
        for trade in self.open_trades.values():
            sl_pips = trade.get("sl_pips", 15)
            lot     = trade.get("lot", 0.01)
            total  += sl_pips * lot * self.pip_value_per_lot
        return (total / equity) * 100.0

    # ==========================================================================
    # AVAILABLE RISK BUDGET
    # ==========================================================================

    def get_available_risk_budget(self, equity: float = None) -> dict:
        """
        Separates realized, floating, and committed risk.
        FIX 7: floating loss included in effective daily loss calculation.
        FIX 10 note: correlation not yet factored into committed risk.
        """
        eq = equity if equity else self.account_balance

        realized  = self.daily_pnl_dollars
        floating  = self.floating_pnl

        committed = 0.0
        for trade in self.open_trades.values():
            sl_pips   = trade.get("sl_pips", 15)
            lot       = trade.get("lot", 0.01)
            committed += sl_pips * lot * self.pip_value_per_lot

        daily_budget  = eq * DAILY_HARD_STOP_PCT
        used_so_far   = abs(min(0.0, realized)) + abs(min(0.0, floating)) + committed
        budget_left   = max(0.0, daily_budget - used_so_far)
        used_risk_pct = (used_so_far / daily_budget * 100) if daily_budget > 0 else 0

        return {
            "realized_pnl":   round(realized, 2),
            "floating_pnl":   round(floating, 2),
            "committed_risk": round(committed, 2),
            "used_risk_pct":  round(used_risk_pct, 1),
            "budget_left":    round(budget_left, 2),
            "safe_to_trade":  budget_left > (eq * RISK_PER_TRADE_PCT),
        }

    # ==========================================================================
    # CONFIDENCE FLOOR
    # ==========================================================================

    def get_confidence_floor(self, base_floor: float = 55.0) -> float:
        penalty = min(
            self.consecutive_losses * CONF_PENALTY_PER_LOSS,
            MAX_CONF_PENALTY
        )
        return base_floor + penalty

    def is_soft_stop_active(self) -> bool:
        if self.daily_pnl_dollars >= 0:
            return False
        if self.day_start_balance <= 0:
            return False
        pct = abs(self.daily_pnl_dollars) / self.day_start_balance
        return DAILY_SOFT_STOP_PCT <= pct < DAILY_HARD_STOP_PCT

    # ==========================================================================
    # EQUITY CURVE PROTECTION
    # ==========================================================================

    def get_equity_curve_factor(self, equity: float) -> float:
        if len(self.equity_history) < 5:
            return 1.0
        recent = [e for _, e in self.equity_history[-20:]]
        avg    = sum(recent) / len(recent)
        if equity >= avg:          return 1.0
        elif equity >= avg * 0.95: return 0.75
        else:                       return 0.50

    # ==========================================================================
    # STATUS
    # ==========================================================================

    def get_status(self) -> str:
        total = self.total_wins + self.total_losses
        wr    = (self.total_wins / total * 100) if total > 0 else 0.0
        soft  = " [SOFT STOP]" if self.is_soft_stop_active() else ""
        return (
            f"Balance: ${self.account_balance:.2f} | "
            f"Peak: ${self.peak_equity:.2f} | "
            f"Float: ${self.floating_pnl:+.2f} | "
            f"Today: {self.daily_pnl_pips:+.1f}pips "
            f"(${self.daily_pnl_dollars:+.2f}) | "
            f"Trades: {self.trades_today} | "
            f"W/L: {self.total_wins}/{self.total_losses} ({wr:.0f}%) | "
            f"Consec: {self.consecutive_losses} | "
            f"Open: {len(self.open_trades)}{soft}"
        )

    def get_full_report(self) -> dict:
        return {
            "balance":           round(self.account_balance, 2),
            "peak_equity":       round(self.peak_equity, 2),
            "floating_pnl":      round(self.floating_pnl, 2),
            "day_start":         round(self.day_start_balance, 2),
            "daily_pnl_pips":    round(self.daily_pnl_pips, 1),
            "daily_pnl_dollars": round(self.daily_pnl_dollars, 2),
            "trades_today":      self.trades_today,
            "total_wins":        self.total_wins,
            "total_losses":      self.total_losses,
            "consecutive_losses":self.consecutive_losses,
            "open_trades":       len(self.open_trades),
            "soft_stop_active":  self.is_soft_stop_active(),
            "conf_floor":        self.get_confidence_floor(),
        }

    # ==========================================================================
    # FIX 9: ATOMIC JSON SAVE WITH LOCK
    # ==========================================================================

    def _save_state(self) -> None:
        """FIX 9: threading.Lock() prevents concurrent write corruption."""
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        state = {
            "account_balance":    self.account_balance,
            "peak_equity":        self.peak_equity,
            "day_start_balance":  self.day_start_balance,
            "daily_pnl_pips":     self.daily_pnl_pips,
            "daily_pnl_dollars":  self.daily_pnl_dollars,
            "floating_pnl":       self.floating_pnl,
            "trades_today":       self.trades_today,
            "day_date":           self.day_date,
            "consecutive_losses": self.consecutive_losses,
            "total_wins":         self.total_wins,
            "total_losses":       self.total_losses,
            "cooldown_until":     self.cooldown_until,
            "pause_until":        self.pause_until,
            "open_trades":        self.open_trades,
            "equity_history":     self.equity_history,
            "starting_balance":   self.starting_balance,
            "saved_at":           datetime.now(tz=timezone.utc).isoformat(),
        }
        tmp = STATE_FILE + ".tmp"
        with self._lock:   # FIX 9
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp, STATE_FILE)
            except Exception as e:
                self._log(f"State save error: {e}")
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _load_state(self) -> bool:
        if not os.path.exists(STATE_FILE):
            return False
        with self._lock:   # FIX 9
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.account_balance    = state.get("account_balance",    DEFAULT_BALANCE)
                self.peak_equity        = state.get("peak_equity",        self.starting_balance)
                self.day_start_balance  = state.get("day_start_balance",  DEFAULT_BALANCE)
                self.daily_pnl_pips     = state.get("daily_pnl_pips",     0.0)
                self.daily_pnl_dollars  = state.get("daily_pnl_dollars",  0.0)
                self.floating_pnl       = state.get("floating_pnl",       0.0)
                self.trades_today       = state.get("trades_today",        0)
                self.day_date           = state.get("day_date",            self._today())
                self.consecutive_losses = state.get("consecutive_losses",  0)
                self.total_wins         = state.get("total_wins",          0)
                self.total_losses       = state.get("total_losses",        0)
                self.cooldown_until     = state.get("cooldown_until",      None)
                self.pause_until        = state.get("pause_until",         None)
                self.open_trades        = state.get("open_trades",         {})
                self.equity_history     = state.get("equity_history",      [])
                self.starting_balance   = state.get("starting_balance",    DEFAULT_BALANCE)
                self._log(f"State loaded from {STATE_FILE}")
                return True
            except Exception as e:
                self._log(f"State load error: {e} -- starting fresh")
                return False

    def reset_state(self) -> None:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        self.__init__(self.starting_balance,
                      self.pip_value_per_lot,
                      self.pip_size)
        self._log("State manually reset")

    def _parse_dt(self, dt_str):
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _log(self, msg: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        print(f"  [RiskEngine {ts}] {msg}")


# ==============================================================================
# UTILITY
# ==============================================================================

def compute_pip_value(symbol_info, pip_size: float = 0.1) -> float:
    """
    Compute actual pip value per lot from MT5 symbol info.
    Call in live_bot.py at startup:
      pip_val = compute_pip_value(mt5.symbol_info(SYMBOL))
      risk    = RiskEngine(acct.balance, pip_val)
    """
    try:
        tick_size  = symbol_info.trade_tick_size
        tick_value = symbol_info.trade_tick_value
        if tick_size > 0:
            return round((pip_size / tick_size) * tick_value, 4)
    except Exception:
        pass
    return PIP_VALUE_DEFAULT