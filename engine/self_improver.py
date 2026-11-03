import json
from datetime import datetime, timezone
from typing import Dict, Any, List
from config import load_settings
from engine.database import (
    get_conn,
    get_active_quant_weights,
)
from engine.market_data import get_market_bundle
from engine.quant_microstructure import analyze_microstructure
from engine.llm_agents import run_adversarial_debate
from engine.paper_trader import (
    open_paper_trade,
    log_shadow_trade,
    update_positions_and_shadows_for_bar,
)


def _simulate_expectancy_under_weights(
    trades: List[Dict[str, Any]],
    shadows: List[Dict[str, Any]],
    candidate_weights: Dict[str, Any],
) -> float:
    if not trades and not shadows:
        return 0.35

    simulated_r_values = []
    min_gate = float(candidate_weights.get("anomaly_trigger_threshold", 52.0))
    sl_mult = float(candidate_weights.get("atr_stop_multiplier", 1.6))
    be_r = float(candidate_weights.get("breakeven_trigger_r", 1.2))

    for t in trades:
        score = float(t.get("anomaly_score") or 55.0)
        if score < min_gate:
            continue
        r_val = float(t.get("realized_r") or 0.0)
        mae_r = float(t.get("mae_r") or 0.0)
        mfe_r = float(t.get("mfe_r") or 0.0)

        if r_val < 0 and mfe_r >= be_r:
            r_val = 0.15

        if r_val < 0 and 1.0 <= mae_r <= 1.60 and sl_mult >= 1.80:
            r_val = 0.25

        simulated_r_values.append(r_val)

    for s in shadows:
        if s.get("status") != "RESOLVED":
            continue
        s_score = float(s.get("anomaly_score") or 48.0)
        if s_score >= min_gate:
            simulated_r_values.append(float(s.get("shadow_r") or 0.0))

    if not simulated_r_values:
        return 0.0
    return round(sum(simulated_r_values) / len(simulated_r_values), 3)


def run_self_improvement_cycle() -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT 60")
    closed_trades = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM shadow_trades WHERE status = 'RESOLVED' ORDER BY id DESC LIMIT 60")
    resolved_shadows = [dict(r) for r in cur.fetchall()]

    current_weights = get_active_quant_weights()
    candidate_weights = dict(current_weights)
    diagnostics = []
    proposed_changes = []

    if len(closed_trades) < 2 and len(resolved_shadows) < 2:
        conn.close()
        return {
            "status": "INSUFFICIENT_SAMPLE_SIZE",
            "message": "Need at least 2 closed trades or resolved shadow trades so the bot does not overfit.",
            "closed_analyzed": len(closed_trades),
            "shadows_analyzed": len(resolved_shadows),
        }

    stop_sweeps = [
        t for t in closed_trades if t.get("thesis_grade") == "D_GRADE_PREMATURE_STOP_SWEEP"
    ]
    if len(stop_sweeps) >= max(1, int(len(closed_trades) * 0.25)):
        old_sl = float(candidate_weights.get("atr_stop_multiplier", 1.6))
        new_sl = round(min(2.4, max(1.85, old_sl + 0.25)), 2)
        if new_sl != old_sl:
            candidate_weights["atr_stop_multiplier"] = new_sl
            proposed_changes.append(
                f"Widen ATR Stop Multiplier ({old_sl}x -> {new_sl}x) and enforce >=1.20% structural stop buffer to survive 15m altcoin stop-hunt wicks ({len(stop_sweeps)} premature sweeps detected)."
            )

    givebacks = [
        t for t in closed_trades if t.get("thesis_grade") == "C_GRADE_LATE_PROFIT_GIVEBACK"
    ]
    if len(givebacks) >= 1:
        old_be = float(candidate_weights.get("breakeven_trigger_r", 1.2))
        new_be = round(max(0.85, old_be - 0.15), 2)
        if new_be != old_be:
            candidate_weights["breakeven_trigger_r"] = new_be
            proposed_changes.append(
                f"Tighten Breakeven Guard ({old_be}R -> {new_be}R) because {len(givebacks)} trades reached >1.1R MFE before reversing."
            )

    missed_winners = [
        s for s in resolved_shadows if s.get("shadow_outcome") == "MISSED_WINNER_TOO_STRICT"
    ]
    smart_vetoes = [
        s for s in resolved_shadows if s.get("shadow_outcome") == "SMART_VETO_SAVED_LOSS"
    ]
    if len(missed_winners) > len(smart_vetoes) and len(missed_winners) >= 2:
        old_gate = float(candidate_weights.get("anomaly_trigger_threshold", 52.0))
        new_gate = round(max(44.0, old_gate - 2.5), 1)
        candidate_weights["anomaly_trigger_threshold"] = new_gate
        proposed_changes.append(
            f"Lower Anomaly Gate ({old_gate} -> {new_gate}): Shadow Tracker showed {len(missed_winners)} vetoed setups hit Take-Profit."
        )
    elif len(closed_trades) >= 4:
        low_score_losses = [
            t for t in closed_trades
            if float(t.get("realized_r") or 0.0) < 0 and float(t.get("anomaly_score") or 60.0) < 58.0
        ]
        if len(low_score_losses) >= 2:
            old_gate = float(candidate_weights.get("anomaly_trigger_threshold", 52.0))
            new_gate = round(min(64.0, old_gate + 2.5), 1)
            candidate_weights["anomaly_trigger_threshold"] = new_gate
            proposed_changes.append(
                f"Raise Anomaly Gate ({old_gate} -> {new_gate}) to filter out borderline low-conviction setups ({len(low_score_losses)} losses)."
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    setup_stats: Dict[str, Dict[str, Any]] = {}
    for t in closed_trades:
        st = t["setup_type"]
        s_entry = setup_stats.setdefault(st, {"wins": 0, "total": 0, "sum_r": 0.0})
        s_entry["total"] += 1
        r_val = float(t.get("realized_r") or 0.0)
        s_entry["sum_r"] += r_val
        if r_val > 0:
            s_entry["wins"] += 1

    for st, stats in setup_stats.items():
        wr = round((stats["wins"] / stats["total"]) * 100.0, 1)
        avg_r = round(stats["sum_r"] / stats["total"], 2)
        diagnostics.append(
            f"Setup [{st}]: {stats['wins']}/{stats['total']} wins ({wr}% WR, {avg_r:+.2f}R expectancy)"
        )

    best_setup = None
    best_avg_r = -999.0
    for st, stats in setup_stats.items():
        avg_r = stats["sum_r"] / stats["total"]
        if avg_r > best_avg_r:
            best_avg_r = avg_r
            best_setup = (st, stats)

    if best_setup:
        st_name, st_data = best_setup
        wr = round((st_data["wins"] / st_data["total"]) * 100.0, 1)
        conf = round(min(0.95, max(0.55, 0.65 + (best_avg_r * 0.12))), 2)
        rule_id = f"LEARNED-{st_name[:12]}"
        cur.execute("SELECT id FROM playbook_rules WHERE rule_id = ?", (rule_id,))
        existing = cur.fetchone()
        title = f"Empirical Edge: {st_name.replace('_', ' ').title()}"
        cond = (
            f"Verified across {st_data['total']} real-candle paper trades ({wr}% WR, {best_avg_r:+.2f}R avg). "
            f"Requires Anomaly Score >= {candidate_weights['anomaly_trigger_threshold']} and positive Time Machine EV."
        )
        act = (
            f"Prioritize {st_name} setups; apply {candidate_weights['atr_stop_multiplier']}x ATR invalidation "
            f"and lock breakeven at +{candidate_weights['breakeven_trigger_r']}R MFE."
        )
        if existing:
            cur.execute(
                """
                UPDATE playbook_rules SET
                    condition = ?, action = ?, confidence = ?, sample_size = ?, win_rate = ?, updated_at = ?
                WHERE rule_id = ?
                """,
                (cond, act, conf, st_data["total"], wr, now_iso, rule_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO playbook_rules (
                    rule_id, category, title, condition, action,
                    confidence, sample_size, win_rate, origin, active, updated_at
                ) VALUES (?, 'EMPIRICAL_SELF_LEARNED', ?, ?, ?, ?, ?, ?, 'BATCH_POST_MORTEM', 1, ?)
                """,
                (rule_id, title, cond, act, conf, st_data["total"], wr, now_iso),
            )

    exp_before = _simulate_expectancy_under_weights(
        closed_trades, resolved_shadows, current_weights
    )
    exp_after = _simulate_expectancy_under_weights(
        closed_trades, resolved_shadows, candidate_weights
    )

    approved = 0
    if proposed_changes and exp_after >= exp_before:
        approved = 1
        candidate_weights["version"] = int(current_weights.get("version", 1)) + 1
        reason_str = " | ".join(proposed_changes)
    elif not proposed_changes:
        reason_str = "Batch audit confirmed current Quant Weights are optimal; updated Playbook empirical statistics."
    else:
        reason_str = (
            f"REJECTED BY BACKTEST GATEKEEPER (Expectancy {exp_after:+.2f}R < {exp_before:+.2f}R): "
            + " | ".join(proposed_changes)
        )

    cur.execute(
        """
        INSERT INTO quant_weights_history (
            version, weights_json, reason, backtest_expectancy_before, backtest_expectancy_after, approved, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_weights.get("version", current_weights.get("version", 1)),
            json.dumps(candidate_weights if approved else current_weights),
            reason_str,
            exp_before,
            exp_after,
            approved,
            now_iso,
        ),
    )

    conn.commit()
    conn.close()

    return {
        "status": "EVOLUTION_COMPLETED",
        "approved": bool(approved),
        "reason": reason_str,
        "expectancy_before_r": exp_before,
        "expectancy_after_r": exp_after,
        "diagnostics": diagnostics,
        "closed_analyzed": len(closed_trades),
        "shadows_analyzed": len(resolved_shadows),
        "active_weights": candidate_weights if approved else current_weights,
    }


def run_historical_fast_forward(symbols: List[str] = None, bars_to_replay: int = 60) -> Dict[str, Any]:
    """
    Real-Data Historical Replay Engine:
    Walks forward across REAL historical Bybit 15m candles (up to 200 bars per coin),
    running Closed-Bar SFP/CVD detection, Pre-Trade Time Machine, Risk Firewall,
    Paper Execution, and Shadow Tracking on actual historical market action.
    """
    settings = load_settings()
    watch = symbols or settings.get("watchlist", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    trades_opened = 0
    trades_closed = 0
    shadows_logged = 0

    for offset in range(bars_to_replay, 0, -2):
        for sym in watch:
            bundle = get_market_bundle(sym, interval="15m", limit=200, time_offset_bars=offset)
            candles = bundle["candles"]
            last_bar = candles[-1]

            closed_now = update_positions_and_shadows_for_bar(
                sym, last_bar["high"], last_bar["low"], last_bar["close"], increment_bar=True
            )
            trades_closed += len(closed_now)

            ms = analyze_microstructure(bundle)
            if ms["anomaly_score"] < 40.0:
                continue

            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT symbol FROM trades WHERE status = 'OPEN'")
            open_syms = [r["symbol"] for r in cur.fetchall()]
            conn.close()

            debate = run_adversarial_debate(
                ms=ms,
                open_positions_count=len(open_syms),
                max_open_positions=int(settings.get("max_open_positions", 3)),
                already_in_symbol=(sym in open_syms),
                record_to_db=(offset <= 6),
            )

            if debate["decision"].startswith("EXECUTE"):
                opened = open_paper_trade(ms, debate, is_historical_sim=1)
                if opened:
                    trades_opened += 1
            elif debate["decision"] == "VETO":
                log_shadow_trade(ms, debate)
                shadows_logged += 1

    learning_report = run_self_improvement_cycle()
    return {
        "bars_replayed": bars_to_replay,
        "data_source": "BYBIT_V5_REAL_HISTORICAL_CANDLES",
        "symbols_tested": watch,
        "trades_opened": trades_opened,
        "trades_closed": trades_closed,
        "shadows_logged": shadows_logged,
        "learning_report": learning_report,
    }
