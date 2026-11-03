import os
import json
from pathlib import Path
from typing import List, Dict, Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "whale_bot.db"
RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_config.json"

DEFAULT_WATCHLIST = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "SUIUSDT",
]

DEFAULT_QUANT_WEIGHTS = {
    "version": 1,
    "anomaly_trigger_threshold": 52.0,
    "min_risk_reward_ratio": 2.2,
    "atr_stop_multiplier": 1.6,
    "atr_tp_multiplier": 3.8,
    "funding_zscore_weight": 1.25,
    "oi_spike_weight": 1.30,
    "liquidity_sweep_weight": 1.45,
    "cvd_divergence_weight": 1.35,
    "trend_counter_fade_penalty": 0.70,
    "breakeven_trigger_r": 1.2,
    "max_leverage_cap": 5.0,
}

DEFAULT_SETTINGS = {
    "llm_provider": os.getenv("LLM_PROVIDER", "gemini"),
    "llm_api_key": os.getenv("LLM_API_KEY", "YOUR_LLM_API_KEY_HERE"),
    "llm_base_url": os.getenv("LLM_BASE_URL", ""),
    "llm_model": os.getenv("LLM_MODEL", "gemini-2.5-flash"),
    "initial_balance_usd": float(os.getenv("INITIAL_BALANCE_USD", "10000.0")),
    "max_risk_per_trade_pct": float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.5")),
    "max_open_positions": int(os.getenv("MAX_OPEN_POSITIONS", "3")),
    "taker_fee_pct": float(os.getenv("TAKER_FEE_PCT", "0.055")),
    "slippage_pct": float(os.getenv("SLIPPAGE_PCT", "0.02")),
    "watchlist": [
        s.strip().upper()
        for s in os.getenv("WATCHLIST", ",".join(DEFAULT_WATCHLIST)).split(",")
        if s.strip()
    ],
    "scan_interval_seconds": int(os.getenv("SCAN_INTERVAL_SECONDS", "300")),
    "auto_pilot_enabled": False,
    "primary_timeframe": "15m",
    "htf_timeframe": "1h",
}


def load_settings() -> Dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if RUNTIME_CONFIG_PATH.exists():
        try:
            with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                settings.update(saved)
        except Exception:
            pass
    return settings


def save_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    current = load_settings()
    for k, v in new_settings.items():
        if k in current and v is not None:
            current[k] = v
    with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    return current


def is_llm_key_configured(settings: Dict[str, Any] = None) -> bool:
    s = settings or load_settings()
    key = (s.get("llm_api_key") or "").strip()
    return bool(key) and key != "YOUR_LLM_API_KEY_HERE" and len(key) > 10
