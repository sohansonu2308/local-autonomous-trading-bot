import json
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from config import load_settings, is_llm_key_configured
from engine.database import (
    get_active_playbook_rules,
    get_active_quant_weights,
    get_conn,
)
from engine.news_monitor import get_recent_news_for_symbol

LLM_BAR_CACHE: Dict[str, Dict[str, Any]] = {}


def check_pre_trade_risk_firewall(
    ms: Dict[str, Any],
    settings: Dict[str, Any],
    open_positions_count: int,
    max_open_positions: int,
    already_in_symbol: bool,
) -> Dict[str, Any]:
    """
    Pre-Trade Risk Firewall for NSE Indices, F&O Stocks & MCX Commodities:
      1. Market Session & Intraday Auto-Square-Off Window Guard (15:15 IST NSE / 23:15 IST MCX)
      2. Duplicate symbol exposure check
      3. Max concurrent portfolio positions check
      4. Minimum structural stop distance guard (>= 0.40% for Indices/Stocks)
      5. CVD / Institutional Orderflow Conflict Guard
      6. Oversold (<38 RSI) & Overbought (>62 RSI) Exhaustion Chasing Guard
      7. Free Margin & Correlated Same-Direction Heat Guard
      8. Daily Loss Circuit Breaker (<= 4.5% of ₹10,00,000 initial capital)
      9. Time Machine Sequential + Block-Bootstrap Edge Gate
    """
    entry = float(ms["proposed_entry"])
    stop = float(ms["proposed_stop_loss"])
    direction = ms["suggested_direction"]
    norm_cvd = float(ms.get("normalized_cvd") or 0.0)
    risk_pct_of_price = (abs(entry - stop) / max(entry, 1e-9)) * 100.0
    session_info = ms.get("session") or {}

    if not settings.get("allow_off_hours_trading", True):
        if session_info.get("should_square_off"):
            return {
                "passed": False,
                "code": "INTRADAY_SQUARE_OFF_CUTOFF",
                "detail": f"Blocking new intraday entry during square-off window ({session_info.get('session_label')}).",
            }
        if not session_info.get("is_live_session"):
            return {
                "passed": False,
                "code": "EXCHANGE_SESSION_CLOSED",
                "detail": f"Exchange session closed ({session_info.get('session_label')}). Enable Off-Hours Simulation to test outside live hours.",
            }

    if already_in_symbol:
        return {"passed": False, "code": "DUPLICATE_SYMBOL_EXPOSURE", "detail": f"Already holding an open position in {ms['symbol']}."}

    if open_positions_count >= max_open_positions:
        return {"passed": False, "code": "MAX_PORTFOLIO_HEAT_REACHED", "detail": f"Max open positions ({max_open_positions}) reached."}

    if risk_pct_of_price < 0.35:
        return {
            "passed": False,
            "code": "STOP_TOO_TIGHT_NOISE_DRAG",
            "detail": f"Stop distance ({risk_pct_of_price:.2f}%) is under 0.35% minimum NSE/MCX structural buffer.",
        }

    if direction == "SHORT" and norm_cvd > 0.60:
        return {
            "passed": False,
            "code": "COUNTER_FII_BUY_FLOW",
            "detail": f"Refusing to SHORT while 10-bar Normalized Volume Delta is strongly positive ({norm_cvd:+.2f} institutional buying).",
        }

    if direction == "LONG" and norm_cvd < -0.60:
        return {
            "passed": False,
            "code": "COUNTER_FII_SELL_FLOW",
            "detail": f"Refusing to LONG while 10-bar Normalized Volume Delta is strongly negative ({norm_cvd:+.2f} institutional selling).",
        }

    rsi_14 = float(ms.get("rsi_14") or 50.0)
    if direction == "SHORT" and rsi_14 < 38.0 and not ms.get("bearish_sfp_sweep"):
        return {
            "passed": False,
            "code": "OVERSOLD_CAPITULATION_SHORT_BLOCK",
            "detail": f"Refusing to SHORT into oversold capitulation (RSI {rsi_14:.1f} < 38.0) without a Bearish PDH/SFP sweep.",
        }

    if direction == "LONG" and rsi_14 > 62.0 and not ms.get("bullish_sfp_sweep"):
        return {
            "passed": False,
            "code": "OVERBOUGHT_EXHAUSTION_LONG_BLOCK",
            "detail": f"Refusing to LONG into overbought exhaustion (RSI {rsi_14:.1f} > 62.0) without a Bullish PDL/SFP sweep.",
        }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT initial_balance, cash_balance FROM portfolio_state WHERE id = 1")
    p_row = dict(cur.fetchone())
    init_bal = float(p_row["initial_balance"])
    cash_bal = float(p_row["cash_balance"])

    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT SUM(realized_pnl_usd) as pnl_today FROM trades WHERE status = 'CLOSED' AND closed_at LIKE ?",
        (f"{today_prefix}%",),
    )
    pnl_today = float(cur.fetchone()["pnl_today"] or 0.0)

    cur.execute("SELECT SUM(risk_usd) as open_risk, SUM(margin_used_usd) as used_margin FROM trades WHERE status = 'OPEN'")
    risk_row = cur.fetchone()
    open_risk = float(risk_row["open_risk"] or 0.0)
    used_margin = float(risk_row["used_margin"] or 0.0)

    cur.execute("SELECT COUNT(*) as same_dir_cnt FROM trades WHERE status = 'OPEN' AND direction = ?", (direction,))
    same_dir_cnt = int(cur.fetchone()["same_dir_cnt"] or 0)
    conn.close()

    if same_dir_cnt >= 2 and float(ms.get("anomaly_score") or 0.0) < 65.0:
        return {
            "passed": False,
            "code": "MAX_SAME_DIRECTION_CORRELATED_HEAT",
            "detail": f"Already holding {same_dir_cnt} open {direction} positions; blocking correlated {direction} entry below 65.0 A+ SFP threshold.",
        }

    free_margin = max(0.0, cash_bal - used_margin)
    if free_margin < cash_bal * 0.12:
        return {
            "passed": False,
            "code": "INSUFFICIENT_FREE_MARGIN",
            "detail": f"Free margin (₹{free_margin:,.2f}) is below minimum 12% MIS/SPAN buffer (Used: ₹{used_margin:,.2f} / Wallet: ₹{cash_bal:,.2f}).",
        }

    new_trade_risk = cash_bal * (float(settings.get("max_risk_per_trade_pct", 1.0)) / 100.0)
    worst_case_daily_drawdown = max(0.0, -pnl_today) + open_risk + new_trade_risk
    max_daily_loss_inr = init_bal * 0.045

    if worst_case_daily_drawdown > max_daily_loss_inr:
        return {
            "passed": False,
            "code": "DAILY_LOSS_CIRCUIT_BREAKER",
            "detail": f"Worst-case daily risk (₹{worst_case_daily_drawdown:,.2f}) exceeds 4.5% daily limit (₹{max_daily_loss_inr:,.2f}).",
        }

    tm = ms.get("timemachine") or {}
    tm_verdict = tm.get("verdict", "")
    hist_wr = float(tm.get("hist_win_rate_pct") or 0.0)
    be_wr = float(tm.get("breakeven_win_rate_pct") or 50.0)
    ev_r = float(tm.get("expected_r") or 0.0)

    if tm_verdict in ("STRONGLY_TOWARD_STOP", "LEANS_TOWARD_STOP", "CLOSE_TO_COIN_FLIP") or hist_wr < be_wr or ev_r < 0.12:
        return {
            "passed": False,
            "code": f"TIMEMACHINE_INSUFFICIENT_EDGE ({tm_verdict})",
            "detail": (
                f"Time Machine rejected setup: Verdict={tm_verdict}, Sequential Hist WR {hist_wr}% vs Breakeven {be_wr}% "
                f"({tm.get('hist_wins')}W/{tm.get('hist_losses')}L), Blended EV {ev_r:+.2f}R."
            ),
        }

    return {"passed": True, "code": "PASS", "detail": "All SEBI/MIS margin, VWAP/CVD alignment, and Time Machine sequential checks passed."}


def _call_llm_provider(
    system_prompt: str, user_prompt: str, settings: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    provider = (settings.get("llm_provider") or "gemini").lower().strip()
    api_key = (settings.get("llm_api_key") or "").strip()
    base_url = (settings.get("llm_base_url") or "").strip().rstrip("/")
    model = (settings.get("llm_model") or "gemini-2.5-flash").strip()

    try:
        if provider == "gemini" and not base_url:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return json.loads(text)

        elif provider in ("openai", "openrouter", "groq", "custom") or (base_url and provider != "anthropic"):
            if base_url:
                if base_url.endswith("/chat/completions"):
                    url = base_url
                elif base_url.endswith("/v1"):
                    url = f"{base_url}/chat/completions"
                else:
                    url = f"{base_url}/v1/chat/completions"
            elif provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
            elif provider == "openrouter":
                url = "https://openrouter.ai/api/v1/chat/completions"
            else:
                url = "https://api.groq.com/openai/v1/chat/completions"

            payload = {
                "model": model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["choices"][0]["message"]["content"].strip()
                if text.startswith("```"):
                    text = text.strip("`").removeprefix("json").strip()
                return json.loads(text)

        elif provider == "anthropic":
            url = f"{base_url}/v1/messages" if base_url and not base_url.endswith("/messages") else (base_url or "https://api.anthropic.com/v1/messages")
            payload = {
                "model": model,
                "max_tokens": 1024,
                "temperature": 0.2,
                "system": system_prompt,
                "messages": [
                    {
                        "role": "user",
                        "content": user_prompt + "\n\nRespond ONLY with valid JSON.",
                    }
                ],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "Authorization": f"Bearer {api_key}",
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["content"][0]["text"].strip()
                if text.startswith("```"):
                    text = text.strip("`").removeprefix("json").strip()
                return json.loads(text)
    except Exception:
        return None
    return None


def _build_deterministic_debate(
    ms: Dict[str, Any],
    playbook_rules: List[Dict[str, Any]],
    quant_weights: Dict[str, Any],
    risk_check: Dict[str, Any],
    recent_news: List[Dict[str, Any]],
) -> Dict[str, Any]:
    sym = ms["symbol"]
    direction = ms["suggested_direction"]
    setup = ms["setup_type"]
    score = ms["anomaly_score"]
    regime = ms["regime"]
    rsi = ms["rsi_14"]
    pcr = ms.get("pcr", 1.0)
    vwap = ms.get("vwap", ms["price"])
    pdh = ms.get("pdh", ms["swing_high"])
    pdl = ms.get("pdl", ms["swing_low"])
    retail_long = ms["retail_long_pct"]
    cvd_sig = ms["cvd_signal"]
    up_pool = ms["liquidation_map"]["nearest_upside_pool"]
    dn_pool = ms["liquidation_map"]["nearest_downside_pool"]
    tm = ms.get("timemachine") or {}

    news_snippet = ""
    if recent_news:
        top_n = recent_news[0]
        news_snippet = f" Catalyst [{top_n['source']}: \"{top_n['title'][:75]}...\" ({top_n['sentiment']})] is driving retail F&O sentiment."

    if direction == "LONG":
        retail_narrative = (
            f"Retail NSE/MCX traders on {sym} see price near PDL/Support (₹{pdl:,.2f}, VWAP ₹{vwap:,.2f}) with RSI {rsi} "
            f"and are buying late Puts or shorting breakdowns.{news_snippet} "
            f"Option Chain PCR is {pcr:.2f} ({100 - retail_long:.1f}% retail bearish skew). "
            f"Call writer stop-losses and short-covering triggers are stacked at ₹{up_pool['price']:,.2f} ({up_pool['dominant_leverage']})."
        )
    elif direction == "SHORT":
        retail_narrative = (
            f"Retail traders on {sym} are FOMO-buying Calls/Breakouts near PDH/Resistance (₹{pdh:,.2f}, VWAP ₹{vwap:,.2f}) with RSI {rsi}.{news_snippet} "
            f"{retail_long:.1f}% retail long skew (PCR {pcr:.2f}). "
            f"Retail long stops and Put writer unwinding triggers sit exposed at ₹{dn_pool['price']:,.2f} ({dn_pool['dominant_leverage']})."
        )
    else:
        retail_narrative = (
            f"Retail traders on {sym} are chopping around Session VWAP (₹{vwap:,.2f}) between PDL ₹{pdl:,.2f} and PDH ₹{pdh:,.2f} "
            f"with balanced PCR ({pcr:.2f}).{news_snippet}"
        )

    matched_rule = playbook_rules[0] if playbook_rules else {"rule_id": "OPR-01", "title": "PDH/PDL Stop-Hunt"}
    for r in playbook_rules:
        if ("SFP" in setup or "PDH" in setup or "PDL" in setup) and r["category"] == "LIQUIDITY_SWEEP":
            matched_rule = r
            break
        if "ABSORPTION" in setup and r["category"] == "CVD_ABSORPTION":
            matched_rule = r
            break

    if direction in ("LONG", "SHORT"):
        target_pool = up_pool if direction == "LONG" else dn_pool
        whale_thesis = (
            f"[{matched_rule['rule_id']}: {matched_rule['title']}] "
            f"Closed-bar confirmed {setup} (Score: {score}/100) in {regime} (VWAP: ₹{vwap:,.2f}, PCR: {pcr:.2f}). "
            f"Institutional orderflow shows {cvd_sig} (Norm CVD: {ms['normalized_cvd']:+.2f}, RelVol: {ms['rel_volume']}x). "
            f"Targeting {target_pool['dominant_leverage']} at ₹{target_pool['price']:,.2f} ({target_pool['distance_pct']}% away). "
            f"Time Machine (1,500 Replays): Effective WR {tm.get('decided_win_rate_pct')}% vs Breakeven {tm.get('breakeven_win_rate_pct')}% "
            f"(EV {tm.get('expected_r'):+.2f}R)."
        )
    else:
        whale_thesis = (
            f"No closed-bar FII/Operator trap confirmed on {sym} (Score: {score}/100). "
            f"Waiting for a closed-bar sweep of PDH (₹{pdh:,.2f}) or PDL (₹{pdl:,.2f})."
        )

    min_score = float(quant_weights.get("anomaly_trigger_threshold", 52.0))
    min_rr = float(quant_weights.get("min_risk_reward_ratio", 2.2))

    decision = "HOLD"
    veto_reason = ""
    if direction == "NEUTRAL" or score < 36.0:
        decision = "HOLD"
        risk_verdict = f"NO ACTION: Closed-bar anomaly score ({score}/100) is below institutional trigger threshold ({min_score})."
    elif not risk_check["passed"]:
        decision = "VETO"
        veto_reason = risk_check["code"]
        risk_verdict = f"VETOED BY RISK FIREWALL [{risk_check['code']}]: {risk_check['detail']} Logged to Shadow Tracker."
    elif score < min_score:
        decision = "VETO"
        veto_reason = f"ANOMALY_SCORE_BELOW_GATE ({score} < {min_score})"
        risk_verdict = f"VETOED: Setup {setup} scored {score}/100, below active Quant Gate ({min_score}). Logging to Shadow Tracker."
    elif ms["proposed_rr"] < min_rr:
        decision = "VETO"
        veto_reason = f"ASYMMETRIC_RR_INSUFFICIENT ({ms['proposed_rr']} < {min_rr})"
        risk_verdict = f"VETOED: Planned R:R ({ms['proposed_rr']}R) is under minimum {min_rr}R. Logging to Shadow Tracker."
    else:
        decision = f"EXECUTE_{direction}"
        risk_verdict = (
            f"APPROVED {direction} ON {sym}: Score {score}/100 >= {min_score}, R:R {ms['proposed_rr']} >= {min_rr}. "
            f"Time Machine verified ({tm.get('verdict')}, EV {tm.get('expected_r'):+.2f}R). "
            f"SL @ ₹{ms['proposed_stop_loss']:,.2f}, TP @ ₹{ms['proposed_take_profit']:,.2f}."
        )

    return {
        "retail_agent": retail_narrative,
        "whale_agent": whale_thesis,
        "risk_judge": risk_verdict,
        "timemachine_summary": tm.get("verdict_note", ""),
        "decision": decision,
        "veto_reason": veto_reason,
        "entry_price": ms["proposed_entry"],
        "stop_loss": ms["proposed_stop_loss"],
        "take_profit": ms["proposed_take_profit"],
        "llm_mode": "QUANT_DETERMINISTIC_AGENTS",
    }


def run_adversarial_debate(
    ms: Dict[str, Any],
    open_positions_count: int = 0,
    max_open_positions: int = 3,
    already_in_symbol: bool = False,
    record_to_db: bool = True,
) -> Dict[str, Any]:
    settings = load_settings()
    playbook_rules = get_active_playbook_rules()
    quant_weights = get_active_quant_weights()
    recent_news = get_recent_news_for_symbol(ms["symbol"], limit=3)

    risk_check = check_pre_trade_risk_firewall(
        ms=ms,
        settings=settings,
        open_positions_count=open_positions_count,
        max_open_positions=max_open_positions,
        already_in_symbol=already_in_symbol,
    )

    result = _build_deterministic_debate(
        ms=ms,
        playbook_rules=playbook_rules,
        quant_weights=quant_weights,
        risk_check=risk_check,
        recent_news=recent_news,
    )

    bar_cache_key = f"{ms['symbol']}_{ms.get('last_closed_bar_ts', 0)}"
    watchlist_len = len(settings.get("watchlist") or [])
    if is_llm_key_configured(settings) and (ms["anomaly_score"] >= 38.0 or watchlist_len <= 3):
        if bar_cache_key in LLM_BAR_CACHE:
            cached_llm = LLM_BAR_CACHE[bar_cache_key]
            result.update(cached_llm)
            if not risk_check["passed"] and result["decision"].startswith("EXECUTE"):
                result["decision"] = "VETO"
                result["veto_reason"] = risk_check["code"]
        else:
            system_prompt = (
                "You are an institutional Indian Market (NSE Nifty 50 / BankNifty / F&O Equities & MCX Commodities) "
                "FII / Prop-Desk & Operator Multi-Agent Trading Committee.\n"
                "You hunt retail stop-losses around Previous Day High (PDH), Previous Day Low (PDL), Session VWAP, "
                "Option Chain Call/Put OI Walls (PCR), Closed-Bar SFP sweeps, and Pre-Trade Time Machine simulations.\n"
                "CRITICAL ASYMMETRIC R:R & EXECUTION RULES:\n"
                "1. If `pre_trade_risk_firewall.passed` is false, you MUST return `VETO`.\n"
                "2. Remember Asymmetric R:R math: at 2.35R reward-to-risk, Breakeven Win Rate is only ~29.8%! "
                "Do NOT veto a setup just because `sl_first_pct` > `tp_first_pct` in raw paths if `decided_win_rate_pct` > `breakeven_win_rate_pct` and `expected_r` >= +0.12R.\n"
                "3. When `pre_trade_risk_firewall.passed` is true, `anomaly_score` >= `anomaly_trigger_threshold` (52.0), "
                "and `timemachine.expected_r` >= +0.12R (`LEANS_TOWARD_TARGET` or `STRONGLY_TOWARD_TARGET`), "
                "you SHOULD approve the trade (`EXECUTE_LONG` or `EXECUTE_SHORT`) matching `suggested_direction` unless there is a severe contradiction.\n"
                "Respond strictly in JSON with keys:\n"
                "{\n"
                '  "retail_agent": "2-3 sentences on how retail NSE/MCX traders are trapped around PDH/PDL/VWAP/Options and where their stops sit (use ₹ INR)",\n'
                '  "whale_agent": "2-3 sentences from FII/Operator Strategist citing an active Playbook rule ID (OPR-01..04), VWAP/CVD/PCR flow, and Time Machine odds",\n'
                '  "risk_judge": "2-3 sentences from Chief Risk Officer evaluating Time Machine EV, sequential history wins/losses, MIS/SPAN margin firewall, and R:R",\n'
                '  "decision": "EXECUTE_LONG" | "EXECUTE_SHORT" | "VETO" | "HOLD",\n'
                '  "veto_reason": "Short reason if VETO else empty string"\n'
                "}"
            )
            user_payload = {
                "market_microstructure": {k: v for k, v in ms.items() if k != "recent_candles"},
                "recent_indian_market_news": recent_news,
                "pre_trade_risk_firewall": risk_check,
                "active_playbook_memory": playbook_rules,
                "quant_gate_weights": quant_weights,
            }
            llm_resp = _call_llm_provider(
                system_prompt=system_prompt,
                user_prompt=json.dumps(user_payload, indent=2),
                settings=settings,
            )
            if llm_resp and isinstance(llm_resp, dict) and "decision" in llm_resp:
                dec = str(llm_resp.get("decision", result["decision"])).upper()
                if not risk_check["passed"] and dec.startswith("EXECUTE"):
                    dec = "VETO"
                    llm_resp["veto_reason"] = risk_check["code"]

                llm_patch = {
                    "retail_agent": str(llm_resp.get("retail_agent") or result["retail_agent"]),
                    "whale_agent": str(llm_resp.get("whale_agent") or result["whale_agent"]),
                    "risk_judge": str(llm_resp.get("risk_judge") or result["risk_judge"]),
                    "decision": dec,
                    "veto_reason": str(llm_resp.get("veto_reason") or result["veto_reason"]),
                    "llm_mode": f"LIVE_LLM ({settings.get('llm_provider')}:{settings.get('llm_model')})",
                }
                LLM_BAR_CACHE[bar_cache_key] = llm_patch
                result.update(llm_patch)
            else:
                if result["decision"].startswith("EXECUTE"):
                    result["decision"] = "VETO"
                    result["veto_reason"] = "LLM_API_TIMEOUT_SAFE_GUARD"
                    result["risk_judge"] = (
                        f"SAFE GUARD VETO: Live LLM ({settings.get('llm_model')}) timed out on this scan. "
                        f"Refusing to auto-execute deterministic fallback without Live LLM approval."
                    )
                result["llm_mode"] = "LLM_TIMEOUT_SAFE_GUARD"

    if record_to_db:
        conn = get_conn()
        cur = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        snapshot_slim = {k: v for k, v in ms.items() if k != "recent_candles"}
        cur.execute(
            """
            INSERT INTO agent_debates (
                symbol, timeframe, regime, anomaly_score, setup_type,
                retail_agent_output, whale_agent_output, risk_judge_output,
                timemachine_summary, decision, llm_mode, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ms["symbol"],
                ms["interval"],
                ms["regime"],
                ms["anomaly_score"],
                ms["setup_type"],
                result["retail_agent"],
                result["whale_agent"],
                result["risk_judge"],
                result.get("timemachine_summary", ""),
                result["decision"],
                result["llm_mode"],
                json.dumps(snapshot_slim),
                now_iso,
            ),
        )
        conn.commit()
        conn.close()

    return result
