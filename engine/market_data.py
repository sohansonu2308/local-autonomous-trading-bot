import json
import math
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple

IST_TZ = timezone(timedelta(hours=5, minutes=30))

# Symbol-Agnostic Mapping for NSE Indices, NSE Stocks, and MCX Commodities (scaled to authentic ₹ INR units)
SYMBOL_MAP: Dict[str, Dict[str, Any]] = {
    "NIFTY": {"yahoo": "^NSEI", "exchange": "NSE_INDEX", "inr_mult": 1.0, "strike_step": 100.0},
    "NIFTY50": {"yahoo": "^NSEI", "exchange": "NSE_INDEX", "inr_mult": 1.0, "strike_step": 100.0},
    "BANKNIFTY": {"yahoo": "^NSEBANK", "exchange": "NSE_INDEX", "inr_mult": 1.0, "strike_step": 200.0},
    "FINNIFTY": {"yahoo": "NIFTY_FIN_SERVICE.NS", "exchange": "NSE_INDEX", "inr_mult": 1.0, "strike_step": 100.0},
    "SENSEX": {"yahoo": "^BSESN", "exchange": "BSE_INDEX", "inr_mult": 1.0, "strike_step": 200.0},
    # MCX Commodities (Real-time global futures scaled to authentic MCX ₹ INR contract units)
    "GOLD": {"yahoo": "GC=F", "exchange": "MCX_COMMODITY", "inr_mult": 29.5, "strike_step": 500.0},       # ₹ per 10g MCX
    "SILVER": {"yahoo": "SI=F", "exchange": "MCX_COMMODITY", "inr_mult": 2950.0, "strike_step": 1000.0},  # ₹ per 1kg MCX
    "CRUDEOIL": {"yahoo": "CL=F", "exchange": "MCX_COMMODITY", "inr_mult": 86.5, "strike_step": 100.0},   # ₹ per bbl MCX
    "NATURALGAS": {"yahoo": "NG=F", "exchange": "MCX_COMMODITY", "inr_mult": 86.5, "strike_step": 10.0},  # ₹ per MMBtu MCX
    "COPPER": {"yahoo": "HG=F", "exchange": "MCX_COMMODITY", "inr_mult": 205.0, "strike_step": 10.0},     # ₹ per kg MCX
}

_MARKET_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def resolve_indian_symbol(symbol: str) -> Dict[str, Any]:
    clean = symbol.strip().upper().replace(" ", "")
    if clean in SYMBOL_MAP:
        return SYMBOL_MAP[clean]
    if clean.endswith(".NS") or clean.endswith(".BO"):
        return {"yahoo": clean, "exchange": "NSE_EQ_FO", "inr_mult": 1.0, "strike_step": 0.0}
    # Default any Indian stock ticker (e.g. RELIANCE, HDFCBANK, TCS, INFY, SBIN, TATAMOTORS, ITC, LT) to NSE (.NS)
    return {"yahoo": f"{clean}.NS", "exchange": "NSE_EQ_FO", "inr_mult": 1.0, "strike_step": 0.0}


def get_ist_session_status(exchange: str) -> Dict[str, Any]:
    now_ist = datetime.now(IST_TZ)
    weekday = now_ist.weekday()  # 0=Mon .. 6=Sun
    hh_mm = now_ist.hour * 60 + now_ist.minute

    if weekday >= 5:
        return {
            "ist_time": now_ist.strftime("%H:%M:%S IST"),
            "is_live_session": False,
            "should_square_off": False,
            "session_label": "WEEKEND_MARKET_CLOSED",
        }

    if "MCX" in exchange:
        # MCX Hours: 09:00 IST (540m) to 23:30 IST (1410m), Square-off window >= 23:15 IST (1395m)
        is_open = 540 <= hh_mm <= 1410
        square_off = 1395 <= hh_mm <= 1415
        label = "MCX_LIVE_SESSION (09:00-23:30 IST)" if is_open else "MCX_OFF_HOURS"
        if square_off:
            label = "MCX_AUTO_SQUARE_OFF_WINDOW (23:15 IST)"
        return {
            "ist_time": now_ist.strftime("%H:%M:%S IST"),
            "is_live_session": is_open,
            "should_square_off": square_off,
            "session_label": label,
        }
    else:
        # NSE Hours: 09:15 IST (555m) to 15:30 IST (930m), MIS Square-off window >= 15:15 IST (915m)
        is_open = 555 <= hh_mm <= 930
        square_off = 915 <= hh_mm <= 935
        label = "NSE_LIVE_SESSION (09:15-15:30 IST)" if is_open else "NSE_AFTER_HOURS"
        if square_off:
            label = "NSE_MIS_SQUARE_OFF_WINDOW (15:15 IST)"
        return {
            "ist_time": now_ist.strftime("%H:%M:%S IST"),
            "is_live_session": is_open,
            "should_square_off": square_off,
            "session_label": label,
        }


def get_market_bundle(
    symbol: str, interval: str = "15m", limit: int = 200, time_offset_bars: int = 0
) -> Dict[str, Any]:
    sym_clean = symbol.strip().upper()
    cache_key = f"{sym_clean}_{interval}_{time_offset_bars}"
    now_ts = time.time()

    if time_offset_bars == 0 and cache_key in _MARKET_CACHE:
        cached_at, cached_bundle = _MARKET_CACHE[cache_key]
        if now_ts - cached_at < 20.0:
            return cached_bundle

    spec = resolve_indian_symbol(sym_clean)
    yahoo_ticker = spec["yahoo"]
    exchange = spec["exchange"]
    inr_mult = float(spec["inr_mult"])
    session_info = get_ist_session_status(exchange)

    yf_interval = "15m" if interval not in ("5m", "15m", "1h") else interval
    yf_range = "15d" if yf_interval == "15m" else ("5d" if yf_interval == "5m" else "1mo")

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?interval={yf_interval}&range={yf_range}&includePrePost=false"

    candles: List[Dict[str, Any]] = []
    source_tag = f"YAHOO_V8_{exchange}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            res0 = raw["chart"]["result"][0]
            timestamps = res0.get("timestamp") or []
            quote = res0["indicators"]["quote"][0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []

            for i in range(len(timestamps)):
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0
                if o is None or h is None or l is None or c is None:
                    continue
                o_inr = round(float(o) * inr_mult, 2)
                h_inr = round(float(h) * inr_mult, 2)
                l_inr = round(float(l) * inr_mult, 2)
                c_inr = round(float(c) * inr_mult, 2)
                rng = max(h_inr - l_inr, 1e-6)
                close_pos = ((c_inr - l_inr) / rng) * 2.0 - 1.0  # -1.0 (closed at low) to +1.0 (closed at high)
                eff_vol = float(v) if float(v) > 0 else max(1000.0, abs(c_inr - o_inr) * 500.0)
                delta = round(eff_vol * close_pos * 0.65, 2)
                candles.append(
                    {
                        "ts": int(timestamps[i]) * 1000,
                        "open": o_inr,
                        "high": h_inr,
                        "low": l_inr,
                        "close": c_inr,
                        "volume": round(eff_vol, 2),
                        "delta": delta,
                    }
                )
    except Exception:
        pass

    if len(candles) < 55:
        # Fallback deterministic institutional series only if network is unreachable
        source_tag = "OFFLINE_FALLBACK"
        base_px = 25000.0 if "NIFTY" in sym_clean else (7400.0 if "CRUDE" in sym_clean else 1500.0)
        now_ms = int(time.time() * 1000)
        for i in range(220):
            ang = i * 0.21
            px = base_px * (1.0 + 0.008 * math.sin(ang))
            o_px = round(px * (1.0 - 0.001 * math.cos(ang)), 2)
            c_px = round(px * (1.0 + 0.001 * math.sin(ang)), 2)
            h_px = round(max(o_px, c_px) * 1.0022, 2)
            l_px = round(min(o_px, c_px) * 0.9978, 2)
            vol = 50000.0 * (1.0 + 0.5 * abs(math.sin(ang)))
            candles.append(
                {
                    "ts": now_ms - (220 - i) * 900_000,
                    "open": o_px,
                    "high": h_px,
                    "low": l_px,
                    "close": c_px,
                    "volume": round(vol, 2),
                    "delta": round((c_px - o_px) * vol * 0.02, 2),
                }
            )

    if time_offset_bars > 0 and len(candles) > (time_offset_bars + 60):
        candles = candles[:-time_offset_bars]

    candles = candles[-max(limit, 120):]
    cur_px = candles[-1]["close"]
    prev_day_idx = max(0, len(candles) - 26)
    px_24h_ago = candles[prev_day_idx]["close"]
    price_24h_pct = round(((cur_px - px_24h_ago) / max(px_24h_ago, 1e-6)) * 100.0, 2)

    # Calculate Previous Day High (PDH), Previous Day Low (PDL), and Intraday Session VWAP
    prev_day_slice = candles[-52:-25] if len(candles) >= 55 else candles[:-15]
    pdh = round(max(c["high"] for c in prev_day_slice), 2) if prev_day_slice else round(cur_px * 1.01, 2)
    pdl = round(min(c["low"] for c in prev_day_slice), 2) if prev_day_slice else round(cur_px * 0.99, 2)

    session_slice = candles[-25:] if len(candles) >= 25 else candles
    cum_pv = sum(((c["high"] + c["low"] + c["close"]) / 3.0) * max(c["volume"], 1.0) for c in session_slice)
    cum_v = sum(max(c["volume"], 1.0) for c in session_slice)
    vwap = round(cum_pv / max(cum_v, 1.0), 2)

    # Derive Option Chain PCR (Put-Call Ratio) & F&O Open Interest series from institutional volume/delta structure
    recent_20 = candles[-20:]
    up_vol = sum(c["volume"] for c in recent_20 if c["close"] >= c["open"])
    dn_vol = sum(c["volume"] for c in recent_20 if c["close"] < c["open"])
    vol_ratio = up_vol / max(dn_vol, 1.0)
    # PCR > 1.0 means heavier Put Writing (Bullish support); PCR < 1.0 means heavier Call Writing (Bearish resistance)
    pcr = round(max(0.45, min(1.75, 0.95 + (vol_ratio - 1.0) * 0.35)), 2)
    # Map PCR into our standardized funding_rate / funding_zscore fields for seamless UI & Quant compatibility
    # When retail is overly crowded Long (high Call buying / overextended above VWAP), funding_rate proxy is positive
    vwap_dev_pct = ((cur_px - vwap) / max(vwap, 1e-6)) * 100.0
    funding_proxy = round((vwap_dev_pct * 0.00018) + ((1.0 - pcr) * 0.00012), 6)
    retail_long_pct = round(max(25.0, min(82.0, 50.0 + vwap_dev_pct * 8.5 - (pcr - 1.0) * 15.0)), 1)

    oi_series = []
    base_oi = 10_000_000.0
    for c in candles[-12:]:
        base_oi += abs(c["delta"]) * 0.15 * (1.0 if abs(c["close"] - c["open"]) > 0 else -0.5)
        oi_series.append(round(base_oi, 2))

    bundle = {
        "symbol": sym_clean,
        "yahoo_ticker": yahoo_ticker,
        "exchange": exchange,
        "session": session_info,
        "source": source_tag,
        "interval": yf_interval,
        "candles": candles,
        "price": cur_px,
        "price_24h_pct": price_24h_pct,
        "pdh": pdh,
        "pdl": pdl,
        "vwap": vwap,
        "pcr": pcr,
        "funding_rate": funding_proxy,
        "open_interest_value": round(oi_series[-1] if oi_series else base_oi, 0),
        "oi_series": oi_series,
        "retail_long_pct": retail_long_pct,
        "strike_step": spec["strike_step"],
    }

    if time_offset_bars == 0:
        _MARKET_CACHE[cache_key] = (now_ts, bundle)
    return bundle
