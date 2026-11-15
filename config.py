import json
import os
from pathlib import Path
from typing import Dict, Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "indian_whale_bot.db"
RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_config.json"
ENV_PATH = BASE_DIR / ".env"


def _load_dotenv() -> Dict[str, str]:
    env_vars: Dict[str, str] = {}
    if ENV_PATH.exists():
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars


DEFAULT_SETTINGS: Dict[str, Any] = {
    "llm_provider": "gemini",
    "llm_api_key": "YOUR_LLM_API_KEY_HERE",
    "llm_base_url": "",
    "llm_model": "gemini-2.5-flash",
    "broker_provider": "YAHOO_NSE_FREE",
    "broker_api_key": "",
    "broker_client_id": "",
    "initial_balance_usd": 1000000.0,  # ₹10,00,000 (₹10 Lakh INR)
    "max_risk_per_trade_pct": 1.0,     # ₹10,000 risk per trade (1.0% of ₹10L)
    "max_open_positions": 3,
    "taker_fee_pct": 0.035,            # All-in Indian Statutory (STT/CTT + NSE/MCX Txn + GST + Stamp + ₹20 Brokerage)
    "slippage_pct": 0.015,             # Realistic NSE/MCX liquid top-of-book slippage
    "watchlist": [
        "NIFTY50",
        "BANKNIFTY",
        "RELIANCE",
        "HDFCBANK",
        "ICICIBANK",
        "SBIN",
        "GOLD",
        "CRUDEOIL",
    ],
    "scan_interval_seconds": 300,
    "auto_pilot_enabled": True,
    "allow_off_hours_trading": True,   # Allows paper testing & simulation outside 09:15-15:30 IST
    "primary_timeframe": "15m",
    "htf_timeframe": "1h",
}

DEFAULT_QUANT_WEIGHTS: Dict[str, Any] = {
    "version": 1,
    "anomaly_trigger_threshold": 52.0,
    "min_risk_reward_ratio": 2.2,
    "atr_stop_multiplier": 1.85,
    "atr_tp_multiplier": 4.2,
    "breakeven_trigger_r": 1.2,
    "max_leverage_cap": 5.0,           # 5x MIS Intraday / SPAN+Exposure F&O Margin
    "liquidity_sweep_weight": 1.45,    # PDH / PDL / ORB / 30-Bar SFP Sweep weight
    "cvd_divergence_weight": 1.35,     # VWAP + Institutional Volume Delta Absorption weight
    "funding_zscore_weight": 1.25,     # Option Chain PCR / OI Skew Z-Score weight
    "oi_spike_weight": 1.30,
    "trend_counter_fade_penalty": 0.65,
}


def load_settings() -> Dict[str, Any]:
    dotenv = _load_dotenv()
    merged = dict(DEFAULT_SETTINGS)

    if dotenv.get("LLM_PROVIDER"):
        merged["llm_provider"] = dotenv["LLM_PROVIDER"]
    if dotenv.get("LLM_API_KEY"):
        merged["llm_api_key"] = dotenv["LLM_API_KEY"]
    if dotenv.get("LLM_BASE_URL"):
        merged["llm_base_url"] = dotenv["LLM_BASE_URL"]
    if dotenv.get("LLM_MODEL"):
        merged["llm_model"] = dotenv["LLM_MODEL"]

    if RUNTIME_CONFIG_PATH.exists():
        try:
            saved = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if v is not None and v != "":
                    merged[k] = v
        except Exception:
            pass

    env_key = os.getenv("LLM_API_KEY")
    if env_key and env_key != "YOUR_LLM_API_KEY_HERE":
        merged["llm_api_key"] = env_key

    return merged


def save_settings(new_values: Dict[str, Any]) -> Dict[str, Any]:
    current = load_settings()
    for k, v in new_values.items():
        if k in DEFAULT_SETTINGS and v is not None:
            current[k] = v
    RUNTIME_CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def is_llm_key_configured(settings: Dict[str, Any] = None) -> bool:
    s = settings or load_settings()
    key = str(s.get("llm_api_key", "")).strip()
    return bool(key) and key != "YOUR_LLM_API_KEY_HERE" and len(key) > 8
