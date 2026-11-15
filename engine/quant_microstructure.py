from typing import Dict, Any, List
from engine.database import get_active_quant_weights
from engine.timemachine import run_time_machine


def _calculate_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return max(candles[-1]["high"] - candles[-1]["low"], 1e-6)
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    recent_trs = trs[-period:]
    return sum(recent_trs) / len(recent_trs)


def _calculate_rsi(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(len(candles) - period, len(candles)):
        diff = candles[i]["close"] - candles[i - 1]["close"]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_option_wall_and_stop_clusters(
    closed_candles: List[Dict[str, Any]],
    current_price: float,
    atr: float,
    pdh: float,
    pdl: float,
    strike_step: float = 0.0,
) -> Dict[str, Any]:
    """
    Indian Institutional Option-Wall & Stop-Loss Magnet Calculator:
    Maps Call Writer Resistance / Short-Covering Pools (Upside) and
    Put Writer Support / Long-Unwinding Stop Pools (Downside) around
    Previous Day High (PDH), Previous Day Low (PDL), Swing Highs/Lows,
    and round F&O Option Strikes.
    """
    window = closed_candles[-45:] if len(closed_candles) >= 45 else closed_candles
    swing_high_30 = max(c["high"] for c in window[-30:])
    swing_low_30 = min(c["low"] for c in window[-30:])

    if strike_step <= 0.0:
        if current_price >= 20000:
            strike_step = 100.0
        elif current_price >= 5000:
            strike_step = 50.0
        elif current_price >= 1000:
            strike_step = 20.0
        elif current_price >= 200:
            strike_step = 5.0
        else:
            strike_step = 1.0

    # Nearest round Call OI strike above and Put OI strike below
    next_call_strike = round((int(current_price // strike_step) + 2) * strike_step, 2)
    next_put_strike = round((int(current_price // strike_step) - 1) * strike_step, 2)

    upside_candidates = [
        {"price": round(max(pdh, current_price + atr * 1.6), 2), "label": "PDH / Call Writer Stop Pool", "score": 84.0},
        {"price": round(max(swing_high_30, current_price + atr * 2.1), 2), "label": "30-Bar High / Short Cover Pool", "score": 78.0},
        {"price": round(max(next_call_strike, current_price + atr * 1.8), 2), "label": f"CE OI Strike Wall ({int(next_call_strike)})", "score": 74.0},
    ]
    downside_candidates = [
        {"price": round(min(pdl, current_price - atr * 1.6), 2), "label": "PDL / Put Writer Stop Pool", "score": 84.0},
        {"price": round(min(swing_low_30, current_price - atr * 2.1), 2), "label": "30-Bar Low / Long Stop Pool", "score": 78.0},
        {"price": round(min(next_put_strike, current_price - atr * 1.8), 2), "label": f"PE OI Strike Wall ({int(next_put_strike)})", "score": 74.0},
    ]

    upside_valid = [u for u in upside_candidates if u["price"] > current_price * 1.002]
    downside_valid = [d for d in downside_candidates if d["price"] < current_price * 0.998]

    nearest_up = min(upside_valid, key=lambda x: abs(x["price"] - current_price)) if upside_valid else {
        "price": round(current_price + atr * 2.2, 2),
        "label": "Call OI Short-Cover Pool",
        "score": 75.0,
    }
    nearest_dn = min(downside_valid, key=lambda x: abs(current_price - x["price"])) if downside_valid else {
        "price": round(current_price - atr * 2.2, 2),
        "label": "Put OI Long-Flush Pool",
        "score": 75.0,
    }

    up_dist_pct = round(((nearest_up["price"] - current_price) / max(current_price, 1e-6)) * 100.0, 2)
    dn_dist_pct = round(((current_price - nearest_dn["price"]) / max(current_price, 1e-6)) * 100.0, 2)

    dominant_magnet = "BALANCED_OI_WALLS"
    if up_dist_pct <= dn_dist_pct * 1.15 and up_dist_pct <= 2.2:
        dominant_magnet = "UPSIDE_SHORT_LIQUIDATIONS"
    elif dn_dist_pct < up_dist_pct and dn_dist_pct <= 2.2:
        dominant_magnet = "DOWNSIDE_LONG_LIQUIDATIONS"

    return {
        "dominant_magnet": dominant_magnet,
        "nearest_upside_pool": {
            "price": nearest_up["price"],
            "distance_pct": up_dist_pct,
            "dominant_leverage": nearest_up["label"],
            "intensity_score": nearest_up["score"],
        },
        "nearest_downside_pool": {
            "price": nearest_dn["price"],
            "distance_pct": dn_dist_pct,
            "dominant_leverage": nearest_dn["label"],
            "intensity_score": nearest_dn["score"],
        },
    }


def analyze_microstructure(bundle: Dict[str, Any]) -> Dict[str, Any]:
    weights = get_active_quant_weights()
    all_candles = bundle["candles"]
    symbol = bundle["symbol"]
    current_price = float(bundle["price"])
    pdh = float(bundle.get("pdh") or current_price * 1.01)
    pdl = float(bundle.get("pdl") or current_price * 0.99)
    vwap = float(bundle.get("vwap") or current_price)
    pcr = float(bundle.get("pcr") or 1.0)

    # Closed-Bar Confirmation: Evaluate completed candles (all_candles[:-1]) to prevent intrabar wick fakeouts
    closed_candles = all_candles[:-1] if len(all_candles) > 35 else all_candles
    last_closed = closed_candles[-1]
    confirmed_close = float(last_closed["close"])
    last_closed_ts = int(last_closed["ts"])

    atr = _calculate_atr(closed_candles, 14)
    atr_pct = (atr / max(confirmed_close, 1e-9)) * 100.0
    rsi_14 = _calculate_rsi(closed_candles, 14)

    # 1. Volatility & FII/DII Trend Regime Detection
    ema_20 = sum(c["close"] for c in closed_candles[-20:]) / 20.0
    ema_50 = sum(c["close"] for c in closed_candles[-50:]) / 50.0
    trend_slope_pct = ((ema_20 - ema_50) / max(ema_50, 1e-9)) * 100.0

    avg_vol_20 = sum(c["volume"] for c in closed_candles[-22:-2]) / 20.0
    recent_vol = max(closed_candles[-1]["volume"], closed_candles[-2]["volume"])
    rel_volume = round(recent_vol / max(avg_vol_20, 1e-9), 2)

    if trend_slope_pct > atr_pct * 1.15 and confirmed_close > ema_20:
        regime = "FII_BULL_TREND_EXPANSION" if rel_volume > 1.4 else "STEADY_BULL_GRIND"
    elif trend_slope_pct < -atr_pct * 1.15 and confirmed_close < ema_20:
        regime = "FII_BEAR_TREND_EXPANSION" if rel_volume > 1.4 else "STEADY_BEAR_GRIND"
    elif atr_pct < 0.22:
        regime = "LOW_VOL_LIQUIDITY_COMPRESSION"
    else:
        regime = "OPERATOR_STOP_HUNT_RANGE"

    # 2. Closed-Bar Multi-Tier Liquidity Sweep Detector (PDH / PDL + 30-Bar Major SFP + 14-Bar Local SFP)
    prior_window_30 = closed_candles[-32:-3]
    swing_high = max(c["high"] for c in prior_window_30)
    swing_low = min(c["low"] for c in prior_window_30)

    prior_window_14 = closed_candles[-16:-2]
    local_swing_high = max(c["high"] for c in prior_window_14)
    local_swing_low = min(c["low"] for c in prior_window_14)

    last_two_closed_high = max(closed_candles[-1]["high"], closed_candles[-2]["high"])
    last_two_closed_low = min(closed_candles[-1]["low"], closed_candles[-2]["low"])
    last_three_closed_high = max(last_two_closed_high, closed_candles[-3]["high"])
    last_three_closed_low = min(last_two_closed_low, closed_candles[-3]["low"])

    # Check if PDH or PDL was swept and reclaimed on a closed bar
    pdh_swept_and_rejected = (last_three_closed_high > pdh) and (confirmed_close < pdh)
    pdl_swept_and_reclaimed = (last_three_closed_low < pdl) and (confirmed_close > pdl)

    bullish_major_sfp = pdl_swept_and_reclaimed or ((last_three_closed_low < swing_low) and (confirmed_close > swing_low))
    bearish_major_sfp = pdh_swept_and_rejected or ((last_three_closed_high > swing_high) and (confirmed_close < swing_high))

    bullish_local_sfp = (
        not bullish_major_sfp
        and (last_two_closed_low < local_swing_low)
        and (confirmed_close > local_swing_low)
        and (last_closed["close"] > last_closed["open"] or last_closed["delta"] > 0)
    )
    bearish_local_sfp = (
        not bearish_major_sfp
        and (last_two_closed_high > local_swing_high)
        and (confirmed_close < local_swing_high)
        and (last_closed["close"] < last_closed["open"] or last_closed["delta"] < 0)
    )

    bullish_sfp_sweep = bullish_major_sfp or bullish_local_sfp
    bearish_sfp_sweep = bearish_major_sfp or bearish_local_sfp

    dist_to_high_atr = (swing_high - confirmed_close) / max(atr, 1e-9)
    dist_to_low_atr = (confirmed_close - swing_low) / max(atr, 1e-9)

    # 3. Institutional Volume Delta (CVD) & Session VWAP Absorption
    recent_10 = closed_candles[-10:]
    price_change_10 = (recent_10[-1]["close"] - recent_10[0]["open"]) / max(atr, 1e-9)
    cvd_10 = sum(c["delta"] for c in recent_10)
    avg_abs_delta = sum(abs(c["delta"]) for c in closed_candles[-30:]) / 30.0
    normalized_cvd = round(cvd_10 / max(avg_abs_delta * 3.0, 1e-9), 2)

    cvd_signal = "NEUTRAL_FLOW"
    if price_change_10 < -0.45 and normalized_cvd > 0.40:
        cvd_signal = "BULLISH_WHALE_ABSORPTION"
    elif price_change_10 > 0.45 and normalized_cvd < -0.40:
        cvd_signal = "BEARISH_WHALE_DISTRIBUTION"
    elif bearish_sfp_sweep and last_closed["delta"] < 0 and normalized_cvd <= 0.25:
        cvd_signal = "BEARISH_SWEEP_REJECTION"
    elif bullish_sfp_sweep and last_closed["delta"] > 0 and normalized_cvd >= -0.25:
        cvd_signal = "BULLISH_SWEEP_RECLAIM"
    elif normalized_cvd >= 0.85:
        cvd_signal = "STRONG_FII_BUYING_FLOW"
    elif normalized_cvd <= -0.85:
        cvd_signal = "STRONG_FII_SELLING_FLOW"

    # 4. Option Chain PCR & VWAP Positioning Z-Score
    funding_rate = float(bundle["funding_rate"])
    funding_zscore = round((funding_rate - 0.00005) / 0.00015, 2)

    oi_series = bundle.get("oi_series") or []
    oi_delta_pct = 0.0
    if len(oi_series) >= 6 and oi_series[-6] > 0:
        oi_delta_pct = round(((oi_series[-1] - oi_series[-6]) / oi_series[-6]) * 100.0, 2)

    retail_long_pct = float(bundle.get("retail_long_pct", 50.0))

    # 5. Option Strike Wall & Stop-Loss Magnet Map
    liq_map = compute_option_wall_and_stop_clusters(
        closed_candles, current_price, atr, pdh, pdl, float(bundle.get("strike_step") or 0.0)
    )

    # 6. Composite Operator / Institutional Trap Classification
    setup_type = "NO_CLEAR_OPERATOR_EDGE"
    suggested_direction = "NEUTRAL"
    raw_score = 18.0

    bull_points = 0.0
    bear_points = 0.0

    if bullish_major_sfp:
        bull_points += 30.0 * weights.get("liquidity_sweep_weight", 1.45)
    elif bullish_local_sfp:
        bull_points += 20.0 * weights.get("liquidity_sweep_weight", 1.45)
    elif dist_to_low_atr < 0.85 and 32.0 <= rsi_14 <= 44.0:
        bull_points += 14.0

    if bearish_major_sfp:
        bear_points += 30.0 * weights.get("liquidity_sweep_weight", 1.45)
    elif bearish_local_sfp:
        bear_points += 20.0 * weights.get("liquidity_sweep_weight", 1.45)
    elif dist_to_high_atr < 0.85 and 56.0 <= rsi_14 <= 68.0:
        bear_points += 14.0

    if cvd_signal in ("BULLISH_WHALE_ABSORPTION", "BULLISH_SWEEP_RECLAIM"):
        bull_points += 22.0 * weights.get("cvd_divergence_weight", 1.35)
        if rel_volume >= 1.25 or normalized_cvd >= 0.95:
            bull_points += 8.0
    elif cvd_signal == "STRONG_FII_BUYING_FLOW" and 38.0 <= rsi_14 <= 60.0 and funding_zscore <= 0.75:
        bull_points += 18.0 * weights.get("cvd_divergence_weight", 1.35)
        if rel_volume >= 1.30:
            bull_points += 6.0
    elif cvd_signal in ("BEARISH_WHALE_DISTRIBUTION", "BEARISH_SWEEP_REJECTION"):
        bear_points += 22.0 * weights.get("cvd_divergence_weight", 1.35)
        if rel_volume >= 1.25 or normalized_cvd <= -0.95:
            bear_points += 8.0
    elif cvd_signal == "STRONG_FII_SELLING_FLOW" and 40.0 <= rsi_14 <= 62.0 and funding_zscore >= -0.75:
        bear_points += 18.0 * weights.get("cvd_divergence_weight", 1.35)
        if rel_volume >= 1.30:
            bear_points += 6.0

    if funding_zscore <= -1.15:
        bull_points += min(24.0, abs(funding_zscore) * 10.5 * weights.get("funding_zscore_weight", 1.25))
    elif funding_zscore >= 1.15:
        bear_points += min(24.0, abs(funding_zscore) * 10.5 * weights.get("funding_zscore_weight", 1.25))

    if abs(oi_delta_pct) > 0.75:
        oi_pts = min(16.0, abs(oi_delta_pct) * 3.5 * weights.get("oi_spike_weight", 1.30))
        if bull_points > bear_points:
            bull_points += oi_pts
        elif bear_points > bull_points:
            bear_points += oi_pts

    if liq_map["dominant_magnet"] == "UPSIDE_SHORT_LIQUIDATIONS":
        up_intensity = float(liq_map["nearest_upside_pool"].get("intensity_score", 50.0))
        bull_points += 15.0 if up_intensity >= 75.0 else 12.0
    elif liq_map["dominant_magnet"] == "DOWNSIDE_LONG_LIQUIDATIONS":
        dn_intensity = float(liq_map["nearest_downside_pool"].get("intensity_score", 50.0))
        bear_points += 15.0 if dn_intensity >= 75.0 else 12.0

    fade_penalty = float(weights.get("trend_counter_fade_penalty", 0.65))
    if regime in ("FII_BULL_TREND_EXPANSION", "STEADY_BULL_GRIND") and bear_points > bull_points:
        bear_points *= fade_penalty
    elif regime in ("FII_BEAR_TREND_EXPANSION", "STEADY_BEAR_GRIND") and bull_points > bear_points:
        bull_points *= fade_penalty

    # Anti-Exhaustion Guard: Never chase oversold (<38 RSI) with shorts or overbought (>62 RSI) with longs!
    if bear_points > bull_points and (rsi_14 < 38.0 or funding_zscore < -0.85) and not bearish_sfp_sweep:
        bear_points *= 0.45
    elif bull_points > bear_points and (rsi_14 > 62.0 or funding_zscore > 0.85) and not bullish_sfp_sweep:
        bull_points *= 0.45

    if normalized_cvd > 0.60 and bear_points > bull_points:
        bear_points *= 0.50
    elif normalized_cvd < -0.60 and bull_points > bear_points:
        bull_points *= 0.50

    if bull_points >= bear_points and bull_points > 24:
        suggested_direction = "LONG"
        raw_score = min(98.0, bull_points)
        if pdl_swept_and_reclaimed:
            setup_type = "PDL_STOP_HUNT_RECLAIM_LONG"
        elif bullish_sfp_sweep:
            setup_type = "BULLISH_SFP_STOP_HUNT_RECLAIM"
        elif "BULLISH" in cvd_signal or "BUYING" in cvd_signal:
            setup_type = "FII_VWAP_ABSORPTION_LONG"
        elif funding_zscore <= -1.15:
            setup_type = "OPTION_SHORT_COVERING_SQUEEZE"
        else:
            setup_type = "UPSIDE_STRIKE_MAGNET_PULL"
    elif bear_points > bull_points and bear_points > 24:
        suggested_direction = "SHORT"
        raw_score = min(98.0, bear_points)
        if pdh_swept_and_rejected:
            setup_type = "PDH_BREAKOUT_TRAP_SHORT"
        elif bearish_sfp_sweep:
            setup_type = "BEARISH_SFP_BREAKOUT_TRAP"
        elif "BEARISH" in cvd_signal or "SELLING" in cvd_signal:
            setup_type = "FII_VWAP_DISTRIBUTION_SHORT"
        elif funding_zscore >= 1.15:
            setup_type = "OPTION_LONG_UNWINDING_FLUSH"
        else:
            setup_type = "DOWNSIDE_STRIKE_MAGNET_PULL"

    sl_mult = max(float(weights.get("atr_stop_multiplier", 1.85)), 1.75)
    # Indices & Stocks have lower ATR% than crypto alts, so floor structural stop at 0.45% of price or 1.85x ATR
    min_structural_stop_dist = max(atr * sl_mult, current_price * 0.0045)

    if suggested_direction == "LONG":
        anchor_low = min(last_two_closed_low, current_price - atr * 0.4)
        stop_loss = round(min(anchor_low - atr * 0.5, current_price - min_structural_stop_dist), 2)
        risk_dist = abs(current_price - stop_loss)
        liq_target = liq_map["nearest_upside_pool"]["price"]
        if (current_price + risk_dist * 2.2) <= liq_target <= (current_price + risk_dist * 2.8):
            take_profit = round(liq_target, 2)
        else:
            take_profit = round(current_price + risk_dist * 2.35, 2)
    else:
        anchor_high = max(last_two_closed_high, current_price + atr * 0.4)
        stop_loss = round(max(anchor_high + atr * 0.5, current_price + min_structural_stop_dist), 2)
        risk_dist = abs(stop_loss - current_price)
        liq_target = liq_map["nearest_downside_pool"]["price"]
        if (current_price - risk_dist * 2.8) <= liq_target <= (current_price - risk_dist * 2.2):
            take_profit = round(liq_target, 2)
        else:
            take_profit = round(current_price - risk_dist * 2.35, 2)

    risk_dist = abs(current_price - stop_loss)
    reward_dist = abs(take_profit - current_price)
    rr_ratio = round(reward_dist / max(risk_dist, 1e-9), 2)

    tm_result = run_time_machine(
        candles=all_candles,
        direction=suggested_direction,
        entry_price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        replays=1500,
        horizon_bars=24,
    )

    return {
        "symbol": symbol,
        "exchange": bundle["exchange"],
        "session": bundle["session"],
        "source": bundle["source"],
        "interval": bundle["interval"],
        "last_closed_bar_ts": last_closed_ts,
        "price": round(current_price, 2),
        "confirmed_close": round(confirmed_close, 2),
        "price_24h_pct": round(bundle["price_24h_pct"], 2),
        "pdh": pdh,
        "pdl": pdl,
        "vwap": vwap,
        "pcr": pcr,
        "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 3),
        "rsi_14": rsi_14,
        "rel_volume": rel_volume,
        "regime": regime,
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "bullish_sfp_sweep": bullish_sfp_sweep,
        "bearish_sfp_sweep": bearish_sfp_sweep,
        "cvd_signal": cvd_signal,
        "normalized_cvd": normalized_cvd,
        "funding_rate_pct": round(funding_rate * 100.0, 4),
        "funding_zscore": funding_zscore,
        "oi_value_usd": round(bundle["open_interest_value"], 0),
        "oi_delta_pct": oi_delta_pct,
        "retail_long_pct": retail_long_pct,
        "retail_short_pct": round(100.0 - retail_long_pct, 1),
        "liquidation_map": liq_map,
        "anomaly_score": round(raw_score, 1),
        "setup_type": setup_type,
        "suggested_direction": suggested_direction,
        "proposed_entry": round(current_price, 2),
        "proposed_stop_loss": stop_loss,
        "proposed_take_profit": take_profit,
        "proposed_rr": rr_ratio,
        "timemachine": tm_result,
        "recent_candles": all_candles[-36:],
    }
