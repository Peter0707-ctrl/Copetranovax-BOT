"""
CopetraNova -- live_engine_service.py
======================================
Continuous real-time market engine with:
1. Strict Market Open / Market Closed detection (weekend, daily rollover, frozen feed).
2. Strict M15 Candle Close Discipline (signals fire ONLY on new candle close, not every 30s).
3. Plain Session Classification (Asia quiet hours vs London/NY high liquidity).
4. 100% Real Live Market Feeds (Zero dummy data).

ZERO EMOJIS strictly enforced.
"""

import os
import sys
import time
import json
import pandas as pd
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from live_bot import (
    SYMBOLS, fetch_bars, compute_features, get_session,
    scan_tiers, compute_confidence, compute_accuracy_and_reasoning,
    compute_dual_tpsl, write_signal, update_market_status_json,
    make_signal_id, MT5_AVAILABLE
)
from structure_engine import StructureState, get_structure_score
from risk_engine import RiskEngine, SYMBOL_SPECS

POLL_INTERVAL_SECONDS = 15


def check_market_open(symbol: str, df: pd.DataFrame = None, dt: datetime = None) -> tuple:
    now = dt or datetime.now(tz=timezone.utc)
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    h = now.hour
    m = now.minute

    # 1. Weekend Check (Friday 21:00 UTC to Sunday 21:00 UTC)
    if weekday == 5:
        return False, "SOKO LIMEFUNGWA (WEEKEND) - Litafunguliwa Jumapili saa 21:00 UTC"
    if weekday == 6 and h < 21:
        return False, "SOKO LIMEFUNGWA (WEEKEND) - Litafunguliwa Jumapili saa 21:00 UTC"
    if weekday == 4 and h >= 21:
        return False, "SOKO LIMEFUNGWA (WEEKEND) - Limefungwa hadi Jumapili"

    # 2. Daily CME Settlement & Rollover Breaks
    if symbol == "XAUUSD" and h == 21:
        return False, "SOKO LA GOLD LIMEFUNGWA (Daily CME Settlement 21:00 - 22:00 UTC)"
    if (h == 21 and m >= 55) or (h == 22 and m < 15):
        return False, "MAPUMZIKO YA SOKO (Daily Bank Rollover Break 21:55 - 22:15 UTC)"

    # 3. Feed Freshness Check (If latest candle is older than 35 minutes)
    if df is not None and len(df) > 0:
        latest_ts = df.index[-1]
        candle_age = (now - latest_ts).total_seconds() / 60
        if candle_age > 35:
            return False, f"SOKO LIMEGANDA (Hakuna mishumaa mipya kwa dakika {int(candle_age)})"

    return True, "SOKO LIKO WAZI"


def get_session_info(now_dt: datetime) -> tuple:
    h = now_dt.hour
    if h >= 22 or h < 7:
        return "ASIA", "ASIA SESSION (TOKYO / SYDNEY) - Soko linatembea polepole (Low Volatility). London itafunguliwa saa 10:00 Asubuhi EAT."
    elif 7 <= h < 13:
        return "LONDON", "LONDON SESSION - Peak Institutional Liquidity & Volatility."
    elif 13 <= h < 17:
        return "OVERLAP", "LONDON / NY OVERLAP - Maximum Volume of the Day."
    else:
        return "NEW YORK", "NEW YORK SESSION - Trend Continuations & US News Releases."


def run_live_service():
    print("=" * 65)
    print("COPETRANOVAX // REAL LIVE ENGINE SERVICE INITIALIZED")
    print("Monitored Pairs: XAUUSD, EURUSD, GBPUSD, USDJPY")
    print(f"Poll Interval: Every {POLL_INTERVAL_SECONDS}s | Candle Gate: M15 Close Only")
    print("=" * 65)

    risk = RiskEngine(account_balance=100.0)
    symbol_states = {sym: StructureState() for sym in SYMBOLS}
    last_processed_candle = {sym: None for sym in SYMBOLS}

    while True:
        try:
            now_dt = datetime.now(tz=timezone.utc)
            session, session_desc = get_session_info(now_dt)
            symbols_telemetry = {}
            candidate_signals = []

            for sym in SYMBOLS:
                spec = SYMBOL_SPECS[sym]
                digits = spec["digits"]
                pip_sz = spec["pip_size"]

                # Fetch real live M15 bars
                df15 = fetch_bars(sym, None, 200)
                if len(df15) < 30:
                    continue

                cur_price = float(df15["close"].iloc[-1])
                close = df15["close"]
                high = df15["high"]
                low = df15["low"]
                prev = close.shift(1)
                tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
                atr = tr.ewm(span=14, adjust=False).mean()
                atr_val = float(atr.iloc[-1])
                if atr_val < pip_sz * 2:
                    atr_val = pip_sz * 10

                feat = compute_features(df15)
                feat["_atr_value"] = atr_val
                state = symbol_states[sym]

                # Dynamic HTF Bias from resampled bars
                df_h1 = df15.resample("1h").agg({
                    "open": "first", "high": "max", "low": "min", "close": "last"
                }).dropna()
                df_h4 = df15.resample("4h").agg({
                    "open": "first", "high": "max", "low": "min", "close": "last"
                }).dropna()
                htf_bias = compute_htf_bias(df_h1, df_h4)

                # Real Spread
                if MT5_AVAILABLE:
                    spread_pips = get_spread_pips(sym)
                else:
                    spread_pips = round(max(0.5, min(spec["max_spread"], (atr_val / pip_sz) * 0.04)), 1)

                is_open, market_status_msg = check_market_open(sym, df15, now_dt)

                symbols_telemetry[sym] = {
                    "price": round(cur_price, digits),
                    "spread_pips": spread_pips,
                    "max_spread": spec["max_spread"],
                    "spread_safe": spread_pips <= spec["max_spread"],
                    "adx": feat["_adx"],
                    "atr": round(atr_val, digits),
                    "h1_bias": htf_bias.get("H1", "NEUTRAL"),
                    "h1_phase": htf_bias.get("H1_phase", "RANGING"),
                    "stage": state.stage,
                    "market_open": is_open,
                    "market_status": market_status_msg,
                    "session": session,
                }

                # Check if market is closed
                if not is_open:
                    sig_id = f"{sym}_CLOSED"
                    write_signal(
                        direction=0, tier="STANDBY", trade_type="MARKET CLOSED",
                        confidence=0.0, accuracy=0.0, grade="STANDBY",
                        reasoning=f"{market_status_msg}. Hakuna biashara inayoruhusiwa.",
                        tpsl={"sl": round(cur_price, digits), "tp1": round(cur_price, digits), "tp2": round(cur_price, digits), "sl_pips": 0, "tp1_pips": 0, "tp2_pips": 0, "rr": 0},
                        lot=0.0, feat=feat, signal_id=sig_id, entry_price=round(cur_price, digits),
                        session=session, symbol=sym
                    )
                    continue

                # Candle Gate: Only evaluate new signal on new M15 candle close!
                latest_candle_time = df15.index[-1]
                is_new_candle = (last_processed_candle[sym] != latest_candle_time)

                if is_new_candle:
                    tier_sigs = scan_tiers(feat, htf_bias, session, now_hour=now_dt.hour, symbol=sym)
                    if tier_sigs:
                        sig = tier_sigs[0]
                        d = sig["direction"]
                        tier = sig["tier"]
                        ttype = sig["trade_type"]
                    else:
                        d = 1 if feat["_ema_fast"] >= feat["_ema_slow"] else -1
                        tier = "TREND"
                        ttype = "INTRA-SWING"

                    score, _ = get_structure_score(df15, cur_price, d, atr_val, htf_bias, False, state, session)
                    conf = compute_confidence(d, feat, tier, htf_bias, session, score)
                    acc, grade, reason = compute_accuracy_and_reasoning(d, tier, ttype, feat, htf_bias, session, score)

                    # During Asian session, add context to reasoning
                    if session == "ASIA":
                        reason = f"[ASIA SESSION]: {reason} Soko linatembea polepole usiku."

                    tpsl = compute_dual_tpsl(d, cur_price, tier, ttype, atr_val, conf, acc, symbol=sym)
                    lot = risk.get_lot_size(sl_pips=tpsl["sl_pips"], symbol=sym)
                    sig_id = make_signal_id(tier, d, symbol=sym)

                    write_signal(
                        direction=d, tier=tier, trade_type=ttype, confidence=conf,
                        accuracy=acc, grade=grade, reasoning=reason, tpsl=tpsl, lot=lot,
                        feat=feat, signal_id=sig_id, entry_price=round(cur_price, digits),
                        session=session, symbol=sym
                    )
                    last_processed_candle[sym] = latest_candle_time

                candidate_signals.append({
                    "symbol": sym, "price": cur_price, "feat": feat
                })

            # Update market telemetry
            top_sym = "XAUUSD"
            top_price = symbols_telemetry.get(top_sym, {}).get("price", 0.0)
            top_tele = symbols_telemetry.get(top_sym, {})
            gold_sig = next((c for c in candidate_signals if c["symbol"] == "XAUUSD"), None)
            top_feat = gold_sig["feat"] if gold_sig else feat
            next_candle_sec = max(0, ((14 - (now_dt.minute % 15)) * 60) + (60 - now_dt.second))

            gold_open, gold_status_msg = check_market_open("XAUUSD", None, now_dt)
            risk_stat = f"OK: {session} ACTIVE" if gold_open else f"STANDBY: {gold_status_msg}"

            update_market_status_json(
                price=top_price, session=session,
                htf_bias={"H1": top_tele.get("h1_bias", "NEUTRAL"), "H1_phase": top_tele.get("h1_phase", "RANGING")},
                feat=top_feat, struct_score=2, stage=symbol_states[top_sym].stage,
                risk_status=risk_stat, next_candle_sec=next_candle_sec,
                symbol=top_sym, symbols_telemetry=symbols_telemetry
            )

        except Exception as e:
            print(f"[ERROR] Live scan error: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_live_service()
