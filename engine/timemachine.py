import random
from typing import Dict, Any, List


def run_time_machine(
    candles: List[Dict[str, Any]],
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    replays: int = 1500,
    horizon_bars: int = 24,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Pre-Trade Time Machine Simulator for NSE Indices, F&O Equities & MCX Commodities:
      1. 2-Bar Block-Bootstrap Monte Carlo path simulation preserving volatility clustering
      2. Conditional Historical Setup Replay on actual past liquidity sweeps & rejections
      3. Indian Statutory Cost (STT/CTT + Brokerage + Slippage) adjusted Breakeven Win Rate
    """
    if direction not in ("LONG", "SHORT") or len(candles) < 30:
        return {
            "replays": 0,
            "horizon_bars": horizon_bars,
            "tp_first_pct": 0.0,
            "sl_first_pct": 0.0,
            "neither_pct": 100.0,
            "decided_win_rate_pct": 0.0,
            "breakeven_win_rate_pct": 50.0,
            "edge_gap_pts": 0.0,
            "expected_r": 0.0,
            "hist_triggers": 0,
            "hist_wins": 0,
            "hist_losses": 0,
            "verdict": "NO_SETUP",
            "verdict_note": "Waiting for directional institutional anomaly.",
        }

    risk_dist = abs(entry_price - stop_loss)
    reward_dist = abs(take_profit - entry_price)
    if risk_dist <= 1e-9:
        risk_dist = max(entry_price * 0.005, 1e-6)

    # Include round-trip Indian statutory charges + slippage (~0.07% total) in effective R
    round_trip_cost_dist = entry_price * 0.0007
    eff_reward_dist = max(reward_dist - round_trip_cost_dist, risk_dist * 0.5)
    eff_risk_dist = risk_dist + round_trip_cost_dist
    planned_r = eff_reward_dist / max(eff_risk_dist, 1e-9)
    breakeven_wr = (1.0 / (1.0 + planned_r)) * 100.0

    closed_bars = candles[:-1] if len(candles) > 35 else candles
    sample_bars = closed_bars[-120:] if len(closed_bars) >= 120 else closed_bars

    ratio_pool = []
    for i in range(1, len(sample_bars)):
        prev_c = max(sample_bars[i - 1]["close"], 1e-9)
        cur = sample_bars[i]
        r_o = cur["open"] / prev_c
        r_h = cur["high"] / prev_c
        r_l = cur["low"] / prev_c
        r_c = cur["close"] / prev_c
        ratio_pool.append((r_o, r_h, r_l, r_c))

    if len(ratio_pool) < 10:
        return {
            "replays": 0,
            "horizon_bars": horizon_bars,
            "tp_first_pct": 0.0,
            "sl_first_pct": 0.0,
            "neither_pct": 100.0,
            "decided_win_rate_pct": 0.0,
            "breakeven_win_rate_pct": round(breakeven_wr, 1),
            "edge_gap_pts": 0.0,
            "expected_r": 0.0,
            "hist_triggers": 0,
            "hist_wins": 0,
            "hist_losses": 0,
            "verdict": "INSUFFICIENT_BARS",
            "verdict_note": "Not enough historical bars.",
        }

    rng = random.Random(seed + int(entry_price * 100))
    pool_len = len(ratio_pool)
    block_size = 2

    tp_hits = 0
    sl_hits = 0
    neither_hits = 0
    total_r = 0.0
    terminal_prices = []

    for _ in range(replays):
        px = entry_price
        outcome = "NEITHER"
        step = 0
        while step < horizon_bars and outcome == "NEITHER":
            block_start = rng.randint(0, max(0, pool_len - block_size))
            for b_offset in range(block_size):
                if step >= horizon_bars:
                    break
                r_o, r_h, r_l, r_c = ratio_pool[(block_start + b_offset) % pool_len]
                bar_h = px * r_h
                bar_l = px * r_l
                px = px * r_c
                step += 1

                if direction == "LONG":
                    if bar_l <= stop_loss:
                        outcome = "STOP"
                        break
                    if bar_h >= take_profit:
                        outcome = "TARGET"
                        break
                else:
                    if bar_h >= stop_loss:
                        outcome = "STOP"
                        break
                    if bar_l <= take_profit:
                        outcome = "TARGET"
                        break

        terminal_prices.append(px)
        if outcome == "TARGET":
            tp_hits += 1
            total_r += planned_r
        elif outcome == "STOP":
            sl_hits += 1
            total_r -= 1.0
        else:
            neither_hits += 1
            mtm_r = (
                (px - entry_price) / risk_dist
                if direction == "LONG"
                else (entry_price - px) / risk_dist
            )
            total_r += mtm_r

    hist_wins = 0
    hist_losses = 0
    hist_neither = 0
    stop_pct = risk_dist / entry_price
    tp_pct = reward_dist / entry_price

    for idx in range(12, max(13, len(closed_bars) - horizon_bars)):
        prior_slice = closed_bars[idx - 10 : idx]
        local_low = min(x["low"] for x in prior_slice)
        local_high = max(x["high"] for x in prior_slice)
        b_cur = closed_bars[idx]
        b_prev = closed_bars[idx - 1]

        if direction == "LONG":
            setup_fired = (min(b_cur["low"], b_prev["low"]) <= local_low * 1.002) and (
                b_cur["close"] > b_cur["open"]
            )
        else:
            setup_fired = (max(b_cur["high"], b_prev["high"]) >= local_high * 0.998) and (
                b_cur["close"] < b_cur["open"]
            )

        if not setup_fired:
            continue

        ref_entry = b_cur["close"]
        h_sl = ref_entry * (1.0 - stop_pct) if direction == "LONG" else ref_entry * (1.0 + stop_pct)
        h_tp = ref_entry * (1.0 + tp_pct) if direction == "LONG" else ref_entry * (1.0 - tp_pct)
        h_out = "NEITHER"

        for fwd in closed_bars[idx + 1 : idx + 1 + horizon_bars]:
            if direction == "LONG":
                if fwd["low"] <= h_sl:
                    h_out = "STOP"
                    break
                if fwd["high"] >= h_tp:
                    h_out = "TARGET"
                    break
            else:
                if fwd["high"] >= h_sl:
                    h_out = "STOP"
                    break
                if fwd["low"] <= h_tp:
                    h_out = "TARGET"
                    break
        if h_out == "TARGET":
            hist_wins += 1
        elif h_out == "STOP":
            hist_losses += 1
        else:
            hist_neither += 1

    tp_first_pct = round((tp_hits / replays) * 100.0, 1)
    sl_first_pct = round((sl_hits / replays) * 100.0, 1)
    neither_pct = round((neither_hits / replays) * 100.0, 1)

    decided = tp_hits + sl_hits
    mc_decided_wr = round((tp_hits / decided) * 100.0, 1) if decided > 0 else 0.0

    hist_decided = hist_wins + hist_losses
    hist_wr = round((hist_wins / hist_decided) * 100.0, 1) if hist_decided >= 3 else mc_decided_wr

    effective_wr = round(0.45 * mc_decided_wr + 0.55 * hist_wr, 1) if hist_decided >= 3 else mc_decided_wr
    edge_gap = round(effective_wr - breakeven_wr, 1)

    hist_ev_r = (
        ((hist_wins * planned_r) - (hist_losses * 1.0)) / max(hist_decided, 1)
        if hist_decided >= 3
        else (total_r / replays)
    )
    mc_ev_r = total_r / replays
    expected_r = round(0.45 * mc_ev_r + 0.55 * hist_ev_r, 2) if hist_decided >= 3 else round(mc_ev_r, 2)

    terminal_prices.sort()
    p5_px = round(terminal_prices[int(0.05 * len(terminal_prices))], 2)
    med_px = round(terminal_prices[int(0.50 * len(terminal_prices))], 2)
    p95_px = round(terminal_prices[int(0.95 * len(terminal_prices))], 2)

    if edge_gap >= 7.0 and expected_r >= 0.20 and hist_wr >= (breakeven_wr - 2.0):
        verdict = "STRONGLY_TOWARD_TARGET"
    elif edge_gap >= 1.5 and expected_r >= 0.10 and hist_wr >= (breakeven_wr - 3.0):
        verdict = "LEANS_TOWARD_TARGET"
    elif edge_gap <= -8.0 or expected_r < -0.05:
        verdict = "STRONGLY_TOWARD_STOP"
    elif edge_gap < 0.0 or expected_r < 0.02:
        verdict = "LEANS_TOWARD_STOP"
    else:
        verdict = "CLOSE_TO_COIN_FLIP"

    verdict_note = (
        f"{replays} Replays ({horizon_bars} bars): TP First {tp_first_pct}% | SL First {sl_first_pct}% | "
        f"Open {neither_pct}%. Effective WR {effective_wr}% (MC {mc_decided_wr}% / Setup Hist {hist_wr}%) vs Breakeven {breakeven_wr:.1f}% "
        f"(Edge Gap: {edge_gap:+.1f} pts, EV: {expected_r:+.2f}R). "
        f"Past Setup Triggers: {hist_wins}W / {hist_losses}L / {hist_neither} Open."
    )

    return {
        "replays": replays,
        "horizon_bars": horizon_bars,
        "tp_first_pct": tp_first_pct,
        "sl_first_pct": sl_first_pct,
        "neither_pct": neither_pct,
        "decided_win_rate_pct": effective_wr,
        "mc_win_rate_pct": mc_decided_wr,
        "hist_win_rate_pct": hist_wr,
        "breakeven_win_rate_pct": round(breakeven_wr, 1),
        "edge_gap_pts": edge_gap,
        "expected_r": expected_r,
        "p5_price": p5_px,
        "median_price": med_px,
        "p95_price": p95_px,
        "hist_triggers": hist_wins + hist_losses + hist_neither,
        "hist_wins": hist_wins,
        "hist_losses": hist_losses,
        "verdict": verdict,
        "verdict_note": verdict_note,
    }
