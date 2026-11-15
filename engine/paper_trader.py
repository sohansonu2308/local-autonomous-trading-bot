from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from config import load_settings
from engine.database import get_conn, get_active_quant_weights
from engine.market_data import resolve_indian_symbol, get_ist_session_status


def _compute_indian_statutory_charges(notional_inr: float, fee_pct: float) -> float:
    """
    Computes realistic Indian intraday / F&O statutory charges (₹ INR):
    Flat ₹20 brokerage per order + STT/CTT + NSE/MCX exchange txn + SEBI + Stamp Duty + 18% GST.
    """
    flat_brokerage = 20.0
    statutory_and_gst = notional_inr * fee_pct
    return round(flat_brokerage + statutory_and_gst, 2)


def get_portfolio_summary() -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM portfolio_state WHERE id = 1")
    p_row = dict(cur.fetchone())

    cur.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id DESC")
    open_trades = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT 100")
    closed_trades = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM shadow_trades ORDER BY id DESC LIMIT 60")
    shadow_trades = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM equity_history ORDER BY id ASC LIMIT 200")
    equity_curve = [dict(r) for r in cur.fetchall()]

    conn.close()

    for t in open_trades:
        m_used = float(t.get("margin_used_usd") or 0.0)
        if m_used <= 0.0:
            lev = max(float(t.get("leverage") or 1.0), 1.0)
            m_used = round(float(t.get("position_size_usd") or 0.0) / lev, 2)
            t["margin_used_usd"] = m_used

    unrealized_pnl = sum(float(t.get("unrealized_pnl_usd") or 0.0) for t in open_trades)
    used_margin_usd = round(sum(float(t.get("margin_used_usd") or 0.0) for t in open_trades), 2)
    total_notional_usd = round(sum(float(t.get("position_size_usd") or 0.0) for t in open_trades), 2)

    cash_bal = float(p_row["cash_balance"])
    initial_bal = float(p_row["initial_balance"])
    free_margin_usd = round(max(0.0, cash_bal - used_margin_usd), 2)
    current_equity = round(cash_bal + unrealized_pnl, 2)
    peak_equity = max(float(p_row["peak_equity"]), current_equity)
    drawdown_pct = round(((peak_equity - current_equity) / max(peak_equity, 1.0)) * 100.0, 2)

    wins = [t for t in closed_trades if float(t.get("realized_pnl_usd") or 0.0) > 0]
    losses = [t for t in closed_trades if float(t.get("realized_pnl_usd") or 0.0) <= 0]
    total_closed = len(closed_trades)
    win_rate = round((len(wins) / total_closed) * 100.0, 1) if total_closed > 0 else 0.0

    gross_profit = sum(float(t["realized_pnl_usd"]) for t in wins)
    gross_loss = abs(sum(float(t["realized_pnl_usd"]) for t in losses))
    profit_factor = (
        round(gross_profit / gross_loss, 2)
        if gross_loss > 0
        else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
    )

    avg_r = (
        round(sum(float(t.get("realized_r") or 0.0) for t in closed_trades) / total_closed, 2)
        if total_closed > 0
        else 0.0
    )

    resolved_shadows = [s for s in shadow_trades if s.get("status") == "RESOLVED"]
    smart_vetoes = [s for s in resolved_shadows if s.get("shadow_outcome") == "SMART_VETO_SAVED_LOSS"]
    veto_accuracy = (
        round((len(smart_vetoes) / len(resolved_shadows)) * 100.0, 1)
        if resolved_shadows
        else 0.0
    )

    return {
        "initial_balance": initial_bal,
        "cash_balance": round(cash_bal, 2),
        "used_margin_usd": used_margin_usd,
        "free_margin_usd": free_margin_usd,
        "total_notional_usd": total_notional_usd,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "current_equity": current_equity,
        "total_return_pct": round(((current_equity - initial_bal) / max(initial_bal, 1.0)) * 100.0, 2),
        "realized_pnl": round(float(p_row["realized_pnl"]), 2),
        "total_fees_paid": round(float(p_row["total_fees_paid"]), 2),
        "drawdown_pct": drawdown_pct,
        "total_closed_trades": total_closed,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "expectancy_r": avg_r,
        "veto_accuracy_pct": veto_accuracy,
        "open_positions": open_trades,
        "closed_trades": closed_trades,
        "shadow_trades": shadow_trades,
        "equity_curve": equity_curve[-80:],
    }


def open_paper_trade(
    ms: Dict[str, Any], debate: Dict[str, Any], is_historical_sim: int = 0
) -> Optional[Dict[str, Any]]:
    settings = load_settings()
    weights = get_active_quant_weights()

    direction = "LONG" if "LONG" in debate["decision"] else "SHORT"
    raw_entry = float(debate["entry_price"])
    stop_loss = float(debate["stop_loss"])
    take_profit = float(debate["take_profit"])

    slip_pct = float(settings.get("slippage_pct", 0.015)) / 100.0
    entry_price = (
        round(raw_entry * (1.0 + slip_pct), 2)
        if direction == "LONG"
        else round(raw_entry * (1.0 - slip_pct), 2)
    )

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM portfolio_state WHERE id = 1")
    p_row = dict(cur.fetchone())
    cash_bal = float(p_row["cash_balance"])

    cur.execute("SELECT position_size_usd, margin_used_usd, leverage, unrealized_pnl_usd FROM trades WHERE status = 'OPEN'")
    open_rows = [dict(r) for r in cur.fetchall()]

    used_margin = 0.0
    open_unrealized = 0.0
    for r in open_rows:
        m = float(r.get("margin_used_usd") or 0.0)
        if m <= 0.0:
            m = float(r.get("position_size_usd") or 0.0) / max(float(r.get("leverage") or 1.0), 1.0)
        used_margin += m
        open_unrealized += float(r.get("unrealized_pnl_usd") or 0.0)

    free_margin = max(0.0, cash_bal - used_margin)
    account_equity = max(10000.0, cash_bal + open_unrealized)

    max_open_pos = max(1, int(settings.get("max_open_positions", 3)))
    slot_share = min(0.30, 0.90 / max_open_pos)
    slot_max_margin_inr = round(min(free_margin * 0.95, cash_bal * slot_share), 2)

    if slot_max_margin_inr < 10000.0:
        conn.close()
        return None

    risk_pct = float(settings.get("max_risk_per_trade_pct", 1.0)) / 100.0
    risk_inr = round(account_equity * risk_pct, 2)

    stop_dist_pct = abs(entry_price - stop_loss) / max(entry_price, 1e-9)
    if stop_dist_pct <= 0.0012:
        conn.close()
        return None

    raw_pos_inr = risk_inr / stop_dist_pct
    max_lev = float(weights.get("max_leverage_cap", 5.0))
    max_slot_notional_inr = round(slot_max_margin_inr * max_lev, 2)
    position_size_inr = round(min(raw_pos_inr, max_slot_notional_inr), 2)

    if position_size_inr <= slot_max_margin_inr:
        leverage = 1.0
        margin_used_inr = position_size_inr
    else:
        margin_used_inr = slot_max_margin_inr
        leverage = round(position_size_inr / max(margin_used_inr, 1.0), 2)

    qty = round(position_size_inr / entry_price, 4)
    actual_risk_inr = round(position_size_inr * stop_dist_pct, 2)
    reward_dist_pct = abs(take_profit - entry_price) / max(entry_price, 1e-9)
    rr_planned = round(reward_dist_pct / max(stop_dist_pct, 1e-9), 2)

    fee_pct = float(settings.get("taker_fee_pct", 0.035)) / 100.0
    entry_fee = _compute_indian_statutory_charges(position_size_inr, fee_pct)
    tm_verdict = (ms.get("timemachine") or {}).get("verdict", "VERIFIED")

    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        INSERT INTO trades (
            symbol, direction, status, setup_type, regime,
            entry_price, current_price, stop_loss, initial_stop_loss, take_profit,
            position_size_usd, margin_used_usd, qty, leverage, risk_usd, rr_planned,
            unrealized_pnl_usd, fees_paid_usd, highest_price, lowest_price,
            bars_held, max_hold_bars, anomaly_score, timemachine_verdict,
            retail_trap_thesis, whale_thesis, risk_judge_verdict,
            opened_at, is_historical_sim
        ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 24, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ms["symbol"],
            direction,
            ms["setup_type"],
            ms["regime"],
            entry_price,
            entry_price,
            stop_loss,
            stop_loss,
            take_profit,
            position_size_inr,
            margin_used_inr,
            qty,
            leverage,
            actual_risk_inr,
            rr_planned,
            0.0,
            entry_fee,
            entry_price,
            entry_price,
            ms["anomaly_score"],
            tm_verdict,
            debate["retail_agent"],
            debate["whale_agent"],
            debate["risk_judge"],
            now_iso,
            is_historical_sim,
        ),
    )
    trade_id = cur.lastrowid

    new_cash = round(cash_bal - entry_fee, 2)
    new_fees = round(float(p_row["total_fees_paid"]) + entry_fee, 2)
    cur.execute(
        "UPDATE portfolio_state SET cash_balance = ?, total_fees_paid = ?, updated_at = ? WHERE id = 1",
        (new_cash, new_fees, now_iso),
    )

    conn.commit()
    cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    created = dict(cur.fetchone())
    conn.close()
    return created


def close_position_at_market(trade_id: int, reason: str = "MANUAL_USER_CLOSE") -> Dict[str, Any]:
    settings = load_settings()
    fee_pct = float(settings.get("taker_fee_pct", 0.035)) / 100.0
    slip_pct = float(settings.get("slippage_pct", 0.015)) / 100.0

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE id = ? AND status = 'OPEN'", (trade_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"status": "NOT_FOUND", "trade_id": trade_id}

    t = dict(row)
    direction = t["direction"]
    entry = float(t["entry_price"])
    cur_px = float(t["current_price"])
    init_sl = float(t["initial_stop_loss"])
    qty = float(t["qty"])
    risk_usd = max(float(t["risk_usd"]), 1e-6)
    entry_fee = float(t["fees_paid_usd"])
    hi = max(float(t["highest_price"]), cur_px)
    lo = min(float(t["lowest_price"]), cur_px)
    bars_held = int(t.get("bars_held") or 0)

    exit_price = (
        round(cur_px * (1.0 - slip_pct), 2)
        if direction == "LONG"
        else round(cur_px * (1.0 + slip_pct), 2)
    )
    gross_pnl = (
        (exit_price - entry) * qty
        if direction == "LONG"
        else (entry - exit_price) * qty
    )
    exit_notional = qty * exit_price
    exit_fee = _compute_indian_statutory_charges(exit_notional, fee_pct)
    total_fees = round(entry_fee + exit_fee, 2)
    net_pnl = round(gross_pnl - total_fees, 2)
    realized_r = round(net_pnl / risk_usd, 2)

    init_risk_dist = max(abs(entry - init_sl), entry * 0.001)
    if direction == "LONG":
        mfe_dist = max(0.0, hi - entry)
        mae_dist = max(0.0, entry - lo)
    else:
        mfe_dist = max(0.0, entry - lo)
        mae_dist = max(0.0, hi - entry)

    mfe_pct = round((mfe_dist / entry) * 100.0, 3)
    mae_pct = round((mae_dist / entry) * 100.0, 3)
    mfe_r = round(mfe_dist / init_risk_dist, 2)
    mae_r = round(mae_dist / init_risk_dist, 2)

    thesis_grade = "A_GRADE_MANUAL_PROFIT_LOCK" if net_pnl > 0 else "B_GRADE_MANUAL_RISK_CUT"
    pm_note = f"Closed ({reason}) at ₹{exit_price:,.2f} ({realized_r:+.2f}R, Net PnL ₹{net_pnl:+,.2f}, MFE {mfe_r}R, MAE {mae_r}R)."
    now_iso = datetime.now(timezone.utc).isoformat()

    cur.execute(
        """
        UPDATE trades SET
            status = 'CLOSED', current_price = ?, exit_price = ?,
            realized_pnl_usd = ?, realized_r = ?, unrealized_pnl_usd = 0.0,
            fees_paid_usd = ?, mae_pct = ?, mfe_pct = ?, mae_r = ?, mfe_r = ?,
            highest_price = ?, lowest_price = ?, bars_held = ?, close_reason = ?,
            thesis_grade = ?, post_mortem_note = ?, closed_at = ?
        WHERE id = ?
        """,
        (
            exit_price,
            exit_price,
            net_pnl,
            realized_r,
            total_fees,
            mae_pct,
            mfe_pct,
            mae_r,
            mfe_r,
            hi,
            lo,
            bars_held,
            reason,
            thesis_grade,
            pm_note,
            now_iso,
            trade_id,
        ),
    )

    cur.execute("SELECT * FROM portfolio_state WHERE id = 1")
    p_row = dict(cur.fetchone())
    new_cash = round(float(p_row["cash_balance"]) + gross_pnl - exit_fee, 2)
    new_realized = round(float(p_row["realized_pnl"]) + net_pnl, 2)
    new_fees = round(float(p_row["total_fees_paid"]) + exit_fee, 2)
    new_peak = max(float(p_row["peak_equity"]), new_cash)
    cur.execute(
        """
        UPDATE portfolio_state
        SET cash_balance = ?, realized_pnl = ?, total_fees_paid = ?, peak_equity = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_cash, new_realized, new_fees, new_peak, now_iso, 1),
    )

    cur.execute("SELECT SUM(unrealized_pnl_usd) as u FROM trades WHERE status = 'OPEN'")
    u_pnl = float(cur.fetchone()["u"] or 0.0)
    eq = round(new_cash + u_pnl, 2)
    pk = max(new_peak, eq)
    dd = round(((pk - eq) / max(pk, 1.0)) * 100.0, 2)
    cur.execute(
        """
        INSERT INTO equity_history (timestamp, equity, cash_balance, unrealized_pnl, drawdown_pct)
        VALUES (?, ?, ?, ?, ?)
        """,
        (now_iso, eq, new_cash, round(u_pnl, 2), dd),
    )

    conn.commit()
    conn.close()
    return {"status": "CLOSED", "trade_id": trade_id, "net_pnl": net_pnl, "realized_r": realized_r}


def log_shadow_trade(ms: Dict[str, Any], debate: Dict[str, Any]) -> None:
    if ms["suggested_direction"] not in ("LONG", "SHORT"):
        return
    conn = get_conn()
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    px = float(ms["proposed_entry"])
    cur.execute(
        """
        INSERT INTO shadow_trades (
            symbol, direction, setup_type, regime, entry_price,
            stop_loss, take_profit, highest_price, lowest_price,
            status, veto_reason, anomaly_score, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'TRACKING', ?, ?, ?)
        """,
        (
            ms["symbol"],
            ms["suggested_direction"],
            ms["setup_type"],
            ms["regime"],
            px,
            float(ms["proposed_stop_loss"]),
            float(ms["proposed_take_profit"]),
            px,
            px,
            debate.get("veto_reason") or "RISK_JUDGE_VETO",
            float(ms["anomaly_score"]),
            now_iso,
        ),
    )
    conn.commit()
    conn.close()


def update_positions_and_shadows_for_bar(
    symbol: str, bar_high: float, bar_low: float, bar_close: float, increment_bar: bool = False
) -> List[Dict[str, Any]]:
    settings = load_settings()
    weights = get_active_quant_weights()
    fee_pct = float(settings.get("taker_fee_pct", 0.035)) / 100.0
    slip_pct = float(settings.get("slippage_pct", 0.015)) / 100.0
    be_trigger_r = float(weights.get("breakeven_trigger_r", 1.2))

    spec = resolve_indian_symbol(symbol)
    session_info = get_ist_session_status(spec["exchange"])
    auto_square_off_now = (not increment_bar) and bool(session_info.get("should_square_off"))

    conn = get_conn()
    cur = conn.cursor()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    cur.execute("SELECT * FROM trades WHERE status = 'OPEN' AND symbol = ?", (symbol,))
    open_rows = [dict(r) for r in cur.fetchall()]
    closed_events = []

    for t in open_rows:
        t_id = t["id"]
        direction = t["direction"]
        entry = float(t["entry_price"])
        sl = float(t["stop_loss"])
        init_sl = float(t["initial_stop_loss"])
        tp = float(t["take_profit"])
        qty = float(t["qty"])
        risk_usd = max(float(t["risk_usd"]), 1e-6)
        entry_fee = float(t["fees_paid_usd"])
        max_hold = int(t.get("max_hold_bars") or 24)

        elapsed_seconds = 9999.0
        try:
            opened_dt = datetime.fromisoformat(str(t["opened_at"]))
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(0.0, (now_dt - opened_dt).total_seconds())
        except Exception:
            pass

        if increment_bar:
            bars_held = int(t.get("bars_held") or 0) + 1
        else:
            bars_held = max(int(t.get("bars_held") or 0), int(elapsed_seconds // 900))

        eff_high = bar_close if (not increment_bar and elapsed_seconds < 900.0) else bar_high
        eff_low = bar_close if (not increment_bar and elapsed_seconds < 900.0) else bar_low

        hi = max(float(t["highest_price"]), eff_high)
        lo = min(float(t["lowest_price"]), eff_low)
        init_risk_dist = max(abs(entry - init_sl), entry * 0.001)

        if direction == "LONG":
            mfe_dist = max(0.0, hi - entry)
            mae_dist = max(0.0, entry - lo)
            raw_unrealized = (bar_close - entry) * qty
        else:
            mfe_dist = max(0.0, entry - lo)
            mae_dist = max(0.0, hi - entry)
            raw_unrealized = (entry - bar_close) * qty

        mfe_pct = round((mfe_dist / entry) * 100.0, 3)
        mae_pct = round((mae_dist / entry) * 100.0, 3)
        mfe_r = round(mfe_dist / init_risk_dist, 2)
        mae_r = round(mae_dist / init_risk_dist, 2)

        hit_sl = (direction == "LONG" and eff_low <= sl) or (direction == "SHORT" and eff_high >= sl)
        hit_tp = (direction == "LONG" and eff_high >= tp) or (direction == "SHORT" and eff_low <= tp)
        hit_time_stop = bars_held >= max_hold

        if hit_sl or hit_tp or hit_time_stop or auto_square_off_now:
            if hit_sl:
                raw_exit = sl
                is_be = abs(sl - entry) < (init_risk_dist * 0.25)
                close_reason = "BREAKEVEN_STOP_PROTECTED" if is_be else "STOP_LOSS_INVALIDATION"
            elif hit_tp:
                raw_exit = tp
                close_reason = "TAKE_PROFIT_STRIKE_POOL_HIT"
            elif auto_square_off_now:
                raw_exit = bar_close
                close_reason = "MIS_INTRADAY_AUTO_SQUARE_OFF_IST"
            else:
                raw_exit = bar_close
                close_reason = f"TIME_STOP_FLAT_AFTER_{max_hold}_BARS"

            exit_price = (
                round(raw_exit * (1.0 - slip_pct), 2)
                if direction == "LONG"
                else round(raw_exit * (1.0 + slip_pct), 2)
            )
            gross_pnl = (
                (exit_price - entry) * qty
                if direction == "LONG"
                else (entry - exit_price) * qty
            )
            exit_notional = qty * exit_price
            exit_fee = _compute_indian_statutory_charges(exit_notional, fee_pct)
            total_fees = round(entry_fee + exit_fee, 2)
            net_pnl = round(gross_pnl - total_fees, 2)
            realized_r = round(net_pnl / risk_usd, 2)

            if realized_r >= 1.2:
                thesis_grade = "A_GRADE_OPERATOR_EXECUTION"
                pm_note = f"Captured {realized_r:+.2f}R into target strike/OI pool (MFE {mfe_r}R, MAE {mae_r}R)."
            elif close_reason == "BREAKEVEN_STOP_PROTECTED":
                thesis_grade = "B_GRADE_PROTECTED_REVERSAL"
                pm_note = f"Reached +{mfe_r}R MFE before reversing; breakeven guard prevented loss."
            elif "SQUARE_OFF" in close_reason or "TIME_STOP" in close_reason:
                thesis_grade = "B_GRADE_SESSION_TIME_EXIT"
                pm_note = f"Closed flat at session/time cutoff ({realized_r:+.2f}R) to avoid overnight gap risk."
            elif mfe_r >= 1.1 and realized_r < 0:
                thesis_grade = "C_GRADE_LATE_PROFIT_GIVEBACK"
                pm_note = f"Trade went +{mfe_r}R in favor before reversing to SL. Breakeven trigger R should be tightened."
            elif mae_r >= 1.0 and mfe_r < 0.4:
                thesis_grade = "D_GRADE_PREMATURE_STOP_SWEEP"
                pm_note = f"Immediate adverse sweep (MAE {mae_r}R, MFE {mfe_r}R). ATR stop multiplier too tight."
            else:
                thesis_grade = "C_GRADE_INVALIDATED_SETUP"
                pm_note = f"Setup invalidated ({realized_r:+.2f}R). Institutional flow absorbed against thesis."

            cur.execute(
                """
                UPDATE trades SET
                    status = 'CLOSED', current_price = ?, exit_price = ?,
                    realized_pnl_usd = ?, realized_r = ?, unrealized_pnl_usd = 0.0,
                    fees_paid_usd = ?, mae_pct = ?, mfe_pct = ?, mae_r = ?, mfe_r = ?,
                    highest_price = ?, lowest_price = ?, bars_held = ?, close_reason = ?,
                    thesis_grade = ?, post_mortem_note = ?, closed_at = ?
                WHERE id = ?
                """,
                (
                    exit_price,
                    exit_price,
                    net_pnl,
                    realized_r,
                    total_fees,
                    mae_pct,
                    mfe_pct,
                    mae_r,
                    mfe_r,
                    hi,
                    lo,
                    bars_held,
                    close_reason,
                    thesis_grade,
                    pm_note,
                    now_iso,
                    t_id,
                ),
            )

            cur.execute("SELECT * FROM portfolio_state WHERE id = 1")
            p_row = dict(cur.fetchone())
            new_cash = round(float(p_row["cash_balance"]) + gross_pnl - exit_fee, 2)
            new_realized = round(float(p_row["realized_pnl"]) + net_pnl, 2)
            new_fees = round(float(p_row["total_fees_paid"]) + exit_fee, 2)
            new_peak = max(float(p_row["peak_equity"]), new_cash)
            cur.execute(
                """
                UPDATE portfolio_state
                SET cash_balance = ?, realized_pnl = ?, total_fees_paid = ?, peak_equity = ?, updated_at = ?
                WHERE id = 1
                """,
                (new_cash, new_realized, new_fees, new_peak, now_iso),
            )
            closed_events.append({"trade_id": t_id, "symbol": symbol, "net_pnl": net_pnl, "realized_r": realized_r})
        else:
            new_sl = sl
            if mfe_r >= be_trigger_r:
                if direction == "LONG" and sl < entry:
                    new_sl = round(entry + (init_risk_dist * 0.15), 2)
                elif direction == "SHORT" and sl > entry:
                    new_sl = round(entry - (init_risk_dist * 0.15), 2)

            cur.execute(
                """
                UPDATE trades SET
                    current_price = ?, stop_loss = ?, unrealized_pnl_usd = ?,
                    mae_pct = ?, mfe_pct = ?, mae_r = ?, mfe_r = ?,
                    highest_price = ?, lowest_price = ?, bars_held = ?
                WHERE id = ?
                """,
                (
                    bar_close,
                    new_sl,
                    round(raw_unrealized, 2),
                    mae_pct,
                    mfe_pct,
                    mae_r,
                    mfe_r,
                    hi,
                    lo,
                    bars_held,
                    t_id,
                ),
            )

    cur.execute("SELECT * FROM shadow_trades WHERE status = 'TRACKING' AND symbol = ?", (symbol,))
    shadows = [dict(r) for r in cur.fetchall()]
    for s in shadows:
        s_id = s["id"]
        s_dir = s["direction"]
        s_entry = float(s["entry_price"])
        s_sl = float(s["stop_loss"])
        s_tp = float(s["take_profit"])
        s_hi = max(float(s["highest_price"]), bar_high)
        s_lo = min(float(s["lowest_price"]), bar_low)
        s_risk = max(abs(s_entry - s_sl), s_entry * 0.001)

        s_hit_sl = (s_dir == "LONG" and bar_low <= s_sl) or (s_dir == "SHORT" and bar_high >= s_sl)
        s_hit_tp = (s_dir == "LONG" and bar_high >= s_tp) or (s_dir == "SHORT" and bar_low <= s_tp)

        if s_hit_sl or s_hit_tp:
            if s_hit_sl:
                outcome = "SMART_VETO_SAVED_LOSS"
                s_r = -1.0
            else:
                outcome = "MISSED_WINNER_TOO_STRICT"
                s_r = round(abs(s_tp - s_entry) / s_risk, 2)
            cur.execute(
                """
                UPDATE shadow_trades SET
                    status = 'RESOLVED', highest_price = ?, lowest_price = ?,
                    shadow_outcome = ?, shadow_r = ?, resolved_at = ?
                WHERE id = ?
                """,
                (s_hi, s_lo, outcome, s_r, now_iso, s_id),
            )
        else:
            cur.execute(
                "UPDATE shadow_trades SET highest_price = ?, lowest_price = ? WHERE id = ?",
                (s_hi, s_lo, s_id),
            )

    if closed_events:
        cur.execute("SELECT * FROM portfolio_state WHERE id = 1")
        p_row = dict(cur.fetchone())
        cur.execute("SELECT SUM(unrealized_pnl_usd) as u FROM trades WHERE status = 'OPEN'")
        u_row = cur.fetchone()
        u_pnl = float(u_row["u"] or 0.0)
        eq = round(float(p_row["cash_balance"]) + u_pnl, 2)
        pk = max(float(p_row["peak_equity"]), eq)
        dd = round(((pk - eq) / max(pk, 1.0)) * 100.0, 2)
        cur.execute(
            """
            INSERT INTO equity_history (timestamp, equity, cash_balance, unrealized_pnl, drawdown_pct)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now_iso, eq, round(float(p_row["cash_balance"]), 2), round(u_pnl, 2), dd),
        )

    conn.commit()
    conn.close()
    return closed_events
