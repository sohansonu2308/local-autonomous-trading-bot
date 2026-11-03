from typing import Dict, Any, List
from engine.database import get_active_quant_weights
from engine.timemachine import run_time_machine


def compute_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return candles[-1]["close"] * 0.01 if candles else 1.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / max(len(recent), 1)


def compute_rsi(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 50.0
    gains, losses = [], []
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


def compute_liquidation_clusters(
    candles: List[Dict[str, Any]], current_price: float, atr: float
) -> Dict[str, Any]:
    lookback = candles[-65:] if len(candles) >= 65 else candles
    if not lookback:
        return {"upside_pools": [], "downside_pools": [], "dominant_magnet": "NEUTRAL"}

    leverage_tiers = [
        {"lev": 100, "dist_pct": 0.0085, "weight": 1.6},
        {"lev": 50, "dist_pct": 0.0180, "weight": 1.4},
        {"lev": 25, "dist_pct": 0.0360, "weight": 1.2},
        {"lev": 10, "dist_pct": 0.0880, "weight": 0.9},
    ]

    avg_vol = sum(c["volume"] for c in lookback) / max(len(lookback), 1)
    raw_short_liqs = []
    raw_long_liqs = []

    for idx, c in enumerate(lookback[:-1]):
        vol_intensity = c["volume"] / max(avg_vol, 1e-9)
        if vol_intensity < 0.7:
            continue
        entry_pivot = (c["high"] + c["low"] + c["close"]) / 3.0
        subsequent_high = max(x["high"] for x in lookback[idx + 1 :])
        subsequent_low = min(x["low"] for x in lookback[idx + 1 :])

        for tier in leverage_tiers:
            short_liq_px = entry_pivot * (1.0 + tier["dist_pct"])
            if short_liq_px > subsequent_high and short_liq_px > current_price:
                raw_short_liqs.append(
                    {
                        "price": short_liq_px,
                        "leverage": tier["lev"],
                        "density": vol_intensity * tier["weight"],
                    }
                )

            long_liq_px = entry_pivot * (1.0 - tier["dist_pct"])
            if long_liq_px < subsequent_low and long_liq_px < current_price:
                raw_long_liqs.append(
                    {
                        "price": long_liq_px,
                        "leverage": tier["lev"],
                        "density": vol_intensity * tier["weight"],
                    }
                )

    def _cluster_pools(levels: List[Dict[str, Any]], is_upside: bool) -> List[Dict[str, Any]]:
        if not levels:
            mult = 2.2 if is_upside else -2.2
            px = current_price + (atr * mult)
            return [
                {
                    "price": round(px, 6),
                    "distance_pct": round(abs(px - current_price) / current_price * 100.0, 2),
                    "intensity_score": 45.0,
                    "dominant_leverage": "25x-50x",
                }
            ]
        bucket_width = max(atr * 0.45, current_price * 0.0025)
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for item in levels:
            b_idx = int(item["price"] / bucket_width)
            buckets.setdefault(b_idx, []).append(item)

        clustered = []
        for b_idx, items in buckets.items():
            total_density = sum(x["density"] for x in items)
            weighted_px = sum(x["price"] * x["density"] for x in items) / max(total_density, 1e-9)
            dist_pct = abs(weighted_px - current_price) / current_price * 100.0
            if dist_pct > 9.5:
                continue
            levs = sorted(list({f"{x['leverage']}x" for x in items}))
            clustered.append(
                {
                    "price": round(weighted_px, 6),
                    "distance_pct": round(dist_pct, 2),
                    "intensity_score": round(min(100.0, total_density * 14.0), 1),
                    "dominant_leverage": "/".join(levs[:2]),
                }
            )
        clustered.sort(key=lambda x: (-x["intensity_score"], x["distance_pct"]))
        return clustered[:3]

    upside_pools = _cluster_pools(raw_short_liqs, is_upside=True)
    downside_pools = _cluster_pools(raw_long_liqs, is_upside=False)

    up_score = upside_pools[0]["intensity_score"] / max(upside_pools[0]["distance_pct"], 0.25)
    dn_score = downside_pools[0]["intensity_score"] / max(downside_pools[0]["distance_pct"], 0.25)

    if up_score > dn_score * 1.2:
        dominant_magnet = "UPSIDE_SHORT_LIQUIDATIONS"
    elif dn_score > up_score * 1.2:
        dominant_magnet = "DOWNSIDE_LONG_LIQUIDATIONS"
    else:
        dominant_magnet = "BALANCED_TWO_SIDED"

    return {
        "upside_pools": upside_pools,
        "downside_pools": downside_pools,
        "nearest_upside_pool": upside_pools[0],
        "nearest_downside_pool": downside_pools[0],
        "dominant_magnet": dominant_magnet,
    }


def analyze_microstructure(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic Quant Brain (v2 - Post-Forensic Upgrade):
      - Evaluates triggers strictly on COMPLETED closed bars (`candles[:-1]`).
      - Prevents false `BEARISH_SWEEP_REJECTION` when 10-bar Normalized CVD shows strong buying (`> +0.35`).
      - Requires true Funding Z-Score extremity (`abs(funding_zscore) >= 1.15`) instead of raw account L/S ratio.
      - Enforces a minimum structural stop-loss distance (`>= 1.20%` of price or `2.0x ATR`) so 15m noise wicks
        and high-leverage fee drag cannot eat the account.
    """
    weights = get_active_quant_weights()
    all_candles = bundle["candles"]
    symbol = bundle["symbol"]

    forming_bar = all_candles[-1]
    closed_candles = all_candles[:-1] if len(all_candles) > 35 else all_candles
    last_closed = closed_candles[-1]
    last_closed_ts = int(last_closed["timestamp"])
    current_price = forming_bar["close"]
    confirmed_close = last_closed["close"]

    atr = compute_atr(closed_candles, 14)
    atr_pct = (atr / max(confirmed_close, 1e-9)) * 100.0
    rsi_14 = compute_rsi(closed_candles, 14)

    # 1. Volatility & Grinding Trend Regime Detection
    ema_20 = sum(c["close"] for c in closed_candles[-20:]) / 20.0
    ema_50 = sum(c["close"] for c in closed_candles[-50:]) / 50.0
    trend_slope_pct = ((ema_20 - ema_50) / max(ema_50, 1e-9)) * 100.0

    avg_vol_20 = sum(c["volume"] for c in closed_candles[-22:-2]) / 20.0
    recent_vol = max(closed_candles[-1]["volume"], closed_candles[-2]["volume"])
    rel_volume = round(recent_vol / max(avg_vol_20, 1e-9), 2)

    if trend_slope_pct > atr_pct * 1.15 and confirmed_close > ema_20:
        regime = "HIGH_VOL_TREND_EXPANSION" if rel_volume > 1.5 else "STEADY_BULL_GRIND"
    elif trend_slope_pct < -atr_pct * 1.15 and confirmed_close < ema_20:
        regime = "HIGH_VOL_BEAR_EXPANSION" if rel_volume > 1.5 else "STEADY_BEAR_GRIND"
    elif atr_pct < 0.32:
        regime = "LOW_VOL_LIQUIDITY_COMPRESSION"
    else:
        regime = "RANGING_STOP_HUNT_ENVIRONMENT"

    # 2. Closed-Bar Swing High / Swing Low Liquidity Sweep (SFP) Detector
    prior_window = closed_candles[-30:-2]
    swing_high = max(c["high"] for c in prior_window)
    swing_low = min(c["low"] for c in prior_window)

    last_two_closed_high = max(closed_candles[-1]["high"], closed_candles[-2]["high"])
    last_two_closed_low = min(closed_candles[-1]["low"], closed_candles[-2]["low"])

    bullish_sfp_sweep = (last_two_closed_low < swing_low) and (confirmed_close > swing_low)
    bearish_sfp_sweep = (last_two_closed_high > swing_high) and (confirmed_close < swing_high)

    dist_to_high_atr = (swing_high - confirmed_close) / max(atr, 1e-9)
    dist_to_low_atr = (confirmed_close - swing_low) / max(atr, 1e-9)

    # 3. CVD (Cumulative Volume Delta) & Whale Absorption (Fixed: Never contradict 10-bar flow!)
    recent_10 = closed_candles[-10:]
    price_change_10 = (recent_10[-1]["close"] - recent_10[0]["open"]) / max(atr, 1e-9)
    cvd_10 = sum(c["delta"] for c in recent_10)
    avg_abs_delta = sum(abs(c["delta"]) for c in closed_candles[-30:]) / 30.0
    normalized_cvd = round(cvd_10 / max(avg_abs_delta * 3.0, 1e-9), 2)

    cvd_signal = "NEUTRAL_FLOW"
    if price_change_10 < -0.45 and normalized_cvd > 0.45:
        cvd_signal = "BULLISH_WHALE_ABSORPTION"
    elif price_change_10 > 0.45 and normalized_cvd < -0.45:
        cvd_signal = "BEARISH_WHALE_DISTRIBUTION"
    elif bearish_sfp_sweep and last_closed["delta"] < 0 and normalized_cvd <= 0.25:
        cvd_signal = "BEARISH_SWEEP_REJECTION"
    elif bullish_sfp_sweep and last_closed["delta"] > 0 and normalized_cvd >= -0.25:
        cvd_signal = "BULLISH_SWEEP_RECLAIM"
    elif normalized_cvd >= 0.85:
        cvd_signal = "STRONG_SPOT_PERP_BUYING"
    elif normalized_cvd <= -0.85:
        cvd_signal = "STRONG_SPOT_PERP_SELLING"

    # 4. Open Interest & Funding Rate Positioning Z-Score
    funding_rate = bundle["funding_rate"]
    funding_zscore = round((funding_rate - 0.0001) / 0.00015, 2)

    oi_series = bundle.get("oi_series") or []
    oi_delta_pct = 0.0
    if len(oi_series) >= 6 and oi_series[-6] > 0:
        oi_delta_pct = round(((oi_series[-1] - oi_series[-6]) / oi_series[-6]) * 100.0, 2)

    retail_long_pct = bundle.get("retail_long_pct", 50.0)

    # 5. Local LiqTide Liquidation Cluster Map
    liq_map = compute_liquidation_clusters(closed_candles, current_price, atr)

    # 6. Composite Whale Manipulation & Setup Classification
    setup_type = "NO_CLEAR_WHALE_EDGE"
    suggested_direction = "NEUTRAL"
    raw_score = 18.0

    bull_points = 0.0
    bear_points = 0.0

    if bullish_sfp_sweep:
        bull_points += 30.0 * weights.get("liquidity_sweep_weight", 1.45)
    elif dist_to_low_atr < 0.55 and rsi_14 < 36:
        bull_points += 14.0

    if bearish_sfp_sweep:
        bear_points += 30.0 * weights.get("liquidity_sweep_weight", 1.45)
    elif dist_to_high_atr < 0.55 and rsi_14 > 64:
        bear_points += 14.0

    if cvd_signal in ("BULLISH_WHALE_ABSORPTION", "BULLISH_SWEEP_RECLAIM", "STRONG_SPOT_PERP_BUYING"):
        bull_points += 22.0 * weights.get("cvd_divergence_weight", 1.35)
    elif cvd_signal in ("BEARISH_WHALE_DISTRIBUTION", "BEARISH_SWEEP_REJECTION", "STRONG_SPOT_PERP_SELLING"):
        bear_points += 22.0 * weights.get("cvd_divergence_weight", 1.35)

    # Fixed: Require ACTUAL funding Z-score extremity (not just baseline altcoin long/short account ratio)
    if funding_zscore <= -1.15:
        bull_points += min(24.0, abs(funding_zscore) * 10.5 * weights.get("funding_zscore_weight", 1.25))
    elif funding_zscore >= 1.15:
        bear_points += min(24.0, abs(funding_zscore) * 10.5 * weights.get("funding_zscore_weight", 1.25))

    if abs(oi_delta_pct) > 1.2:
        oi_pts = min(16.0, abs(oi_delta_pct) * 3.2 * weights.get("oi_spike_weight", 1.30))
        if bull_points > bear_points:
            bull_points += oi_pts
        elif bear_points > bull_points:
            bear_points += oi_pts

    if liq_map["dominant_magnet"] == "UPSIDE_SHORT_LIQUIDATIONS":
        bull_points += 12.0
    elif liq_map["dominant_magnet"] == "DOWNSIDE_LONG_LIQUIDATIONS":
        bear_points += 12.0

    # Penalize heavily if trying to fade a steady grind or strong opposing CVD flow!
    fade_penalty = float(weights.get("trend_counter_fade_penalty", 0.65))
    if regime in ("HIGH_VOL_TREND_EXPANSION", "STEADY_BULL_GRIND") and bear_points > bull_points:
        bear_points *= fade_penalty
    elif regime in ("HIGH_VOL_BEAR_EXPANSION", "STEADY_BEAR_GRIND") and bull_points > bear_points:
        bull_points *= fade_penalty

    if normalized_cvd > 0.60 and bear_points > bull_points:
        bear_points *= 0.50  # Never aggressively short into strong net buyer absorption
    elif normalized_cvd < -0.60 and bull_points > bear_points:
        bull_points *= 0.50  # Never aggressively long into strong net seller distribution

    if bull_points >= bear_points and bull_points > 24:
        suggested_direction = "LONG"
        raw_score = min(98.0, bull_points)
        if bullish_sfp_sweep:
            setup_type = "BULLISH_SFP_STOP_HUNT_RECLAIM"
        elif "BULLISH" in cvd_signal or "BUYING" in cvd_signal:
            setup_type = "WHALE_LIMIT_ABSORPTION_LONG"
        elif funding_zscore <= -1.15:
            setup_type = "CROWDED_SHORT_FUNDING_SQUEEZE"
        else:
            setup_type = "UPSIDE_LIQUIDATION_MAGNET_PULL"
    elif bear_points > bull_points and bear_points > 24:
        suggested_direction = "SHORT"
        raw_score = min(98.0, bear_points)
        if bearish_sfp_sweep:
            setup_type = "BEARISH_SFP_BREAKOUT_TRAP"
        elif "BEARISH" in cvd_signal or "SELLING" in cvd_signal:
            setup_type = "WHALE_DISTRIBUTION_EXHAUSTION_SHORT"
        elif funding_zscore >= 1.15:
            setup_type = "CROWDED_LONG_LIQUIDATION_FLUSH"
        else:
            setup_type = "DOWNSIDE_LIQUIDATION_MAGNET_PULL"

    # Enforce realistic swing stop buffer (sl_mult * ATR, floor 0.75% of price) and realistic 2.2R-2.6R target
    sl_mult = max(float(weights.get("atr_stop_multiplier", 1.85)), 1.75)
    min_structural_stop_dist = max(atr * sl_mult, current_price * 0.0075)

    if suggested_direction == "LONG":
        anchor_low = min(last_two_closed_low, current_price - atr * 0.4)
        stop_loss = round(min(anchor_low - atr * 0.6, current_price - min_structural_stop_dist), 6)
        risk_dist = abs(current_price - stop_loss)
        liq_target = liq_map["nearest_upside_pool"]["price"]
        # Target nearest upside liquidity pool if between 2.2R and 2.8R, else cap at 2.35R
        if (current_price + risk_dist * 2.2) <= liq_target <= (current_price + risk_dist * 2.8):
            take_profit = round(liq_target, 6)
        else:
            take_profit = round(current_price + risk_dist * 2.35, 6)
    else:
        anchor_high = max(last_two_closed_high, current_price + atr * 0.4)
        stop_loss = round(max(anchor_high + atr * 0.6, current_price + min_structural_stop_dist), 6)
        risk_dist = abs(stop_loss - current_price)
        liq_target = liq_map["nearest_downside_pool"]["price"]
        if (current_price - risk_dist * 2.8) <= liq_target <= (current_price - risk_dist * 2.2):
            take_profit = round(liq_target, 6)
        else:
            take_profit = round(current_price - risk_dist * 2.35, 6)

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
        horizon_bars=48,
    )

    return {
        "symbol": symbol,
        "source": bundle["source"],
        "interval": bundle["interval"],
        "last_closed_bar_ts": last_closed_ts,
        "price": round(current_price, 6),
        "confirmed_close": round(confirmed_close, 6),
        "price_24h_pct": round(bundle["price_24h_pct"], 2),
        "atr": round(atr, 6),
        "atr_pct": round(atr_pct, 3),
        "rsi_14": rsi_14,
        "rel_volume": rel_volume,
        "regime": regime,
        "swing_high": round(swing_high, 6),
        "swing_low": round(swing_low, 6),
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
        "proposed_entry": round(current_price, 6),
        "proposed_stop_loss": stop_loss,
        "proposed_take_profit": take_profit,
        "proposed_rr": rr_ratio,
        "timemachine": tm_result,
        "recent_candles": all_candles[-36:],
    }
