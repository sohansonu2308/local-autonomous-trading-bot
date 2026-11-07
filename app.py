import json
import os
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List

from config import (
    BASE_DIR,
    load_settings,
    save_settings,
    is_llm_key_configured,
)
from engine.database import (
    init_db,
    get_conn,
    get_active_playbook_rules,
    get_active_quant_weights,
)
from engine.market_data import get_market_bundle
from engine.quant_microstructure import analyze_microstructure
from engine.llm_agents import run_adversarial_debate
from engine.paper_trader import (
    get_portfolio_summary,
    open_paper_trade,
    log_shadow_trade,
    update_positions_and_shadows_for_bar,
)
from engine.self_improver import (
    run_self_improvement_cycle,
    run_historical_fast_forward,
)
from engine.news_monitor import fetch_and_store_crypto_news

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

LATEST_MARKET_SCANS: Dict[str, Dict[str, Any]] = {}
SCAN_LOCK = threading.Lock()


def execute_watchlist_scan(trigger_debates: bool = True) -> Dict[str, Any]:
    with SCAN_LOCK:
        settings = load_settings()
        watchlist = settings.get("watchlist", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        tf = settings.get("primary_timeframe", "15m")
        max_pos = int(settings.get("max_open_positions", 3))

        # Refresh deduplicated live RSS crypto headlines
        fetch_and_store_crypto_news(max_items=20)

        scan_results = []
        debates_triggered = []
        trades_opened = []

        for sym in watchlist:
            bundle = get_market_bundle(sym, interval=tf, limit=200, time_offset_bars=0)
            candles = bundle["candles"]
            last_bar = candles[-1]

            update_positions_and_shadows_for_bar(
                sym, last_bar["high"], last_bar["low"], last_bar["close"], increment_bar=False
            )

            ms = analyze_microstructure(bundle)
            LATEST_MARKET_SCANS[sym] = ms
            scan_results.append(ms)

            # Always run a debate if watchlist has <= 3 coins (focused testing) or anomaly_score >= 36.0
            should_debate = trigger_debates and (len(watchlist) <= 3 or ms["anomaly_score"] >= 36.0)
            if should_debate:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT symbol FROM trades WHERE status = 'OPEN'")
                open_syms = [r["symbol"] for r in cur.fetchall()]
                conn.close()

                debate = run_adversarial_debate(
                    ms=ms,
                    open_positions_count=len(open_syms),
                    max_open_positions=max_pos,
                    already_in_symbol=(sym in open_syms),
                    record_to_db=True,
                )
                debates_triggered.append({"symbol": sym, "debate": debate})

                if debate["decision"].startswith("EXECUTE"):
                    opened = open_paper_trade(ms, debate, is_historical_sim=0)
                    if opened:
                        trades_opened.append(opened)
                elif debate["decision"] == "VETO":
                    log_shadow_trade(ms, debate)

        return {
            "scanned_count": len(scan_results),
            "debates_count": len(debates_triggered),
            "trades_opened": len(trades_opened),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def get_full_dashboard_payload() -> Dict[str, Any]:
    settings = load_settings()
    if not LATEST_MARKET_SCANS:
        execute_watchlist_scan(trigger_debates=True)

    portfolio = get_portfolio_summary()
    playbook = get_active_playbook_rules()
    quant_weights = get_active_quant_weights()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM agent_debates ORDER BY id DESC LIMIT 25")
    debates = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM quant_weights_history ORDER BY id DESC LIMIT 15")
    weights_history = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM news_items ORDER BY id DESC LIMIT 20")
    news_items = [dict(r) for r in cur.fetchall()]
    conn.close()

    masked_settings = dict(settings)
    masked_settings["llm_key_configured"] = is_llm_key_configured(settings)

    watchlist = settings.get("watchlist", [])
    scanner_rows = [
        LATEST_MARKET_SCANS[s]
        for s in watchlist
        if s in LATEST_MARKET_SCANS
    ]

    return {
        "settings": masked_settings,
        "portfolio": portfolio,
        "scanner": scanner_rows,
        "debates": debates,
        "playbook": playbook,
        "quant_weights": quant_weights,
        "weights_history": weights_history,
        "news": news_items,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def reset_paper_account() -> Dict[str, Any]:
    settings = load_settings()
    initial_bal = float(settings.get("initial_balance_usd", 10000.0))
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM trades")
    cur.execute("DELETE FROM shadow_trades")
    cur.execute("DELETE FROM agent_debates")
    cur.execute("DELETE FROM equity_history")
    cur.execute(
        """
        UPDATE portfolio_state
        SET initial_balance = ?, cash_balance = ?, realized_pnl = 0.0, total_fees_paid = 0.0, peak_equity = ?, updated_at = ?
        WHERE id = 1
        """,
        (initial_bal, initial_bal, initial_bal, now_iso),
    )
    cur.execute(
        """
        INSERT INTO equity_history (timestamp, equity, cash_balance, unrealized_pnl, drawdown_pct)
        VALUES (?, ?, ?, 0.0, 0.0)
        """,
        (now_iso, initial_bal, initial_bal),
    )
    conn.commit()
    conn.close()
    return {"status": "ACCOUNT_RESET", "initial_balance": initial_bal}


def manual_close_position(trade_id: int) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE id = ? AND status = 'OPEN'", (trade_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"status": "NOT_FOUND"}
    t = dict(row)
    sym = t["symbol"]
    px = float(t["current_price"])
    conn.close()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE trades SET stop_loss = ? WHERE id = ?", (px, trade_id))
    conn.commit()
    conn.close()
    update_positions_and_shadows_for_bar(sym, px, px, px)
    return {"status": "CLOSED", "trade_id": trade_id}


class WhaleDashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            index_file = STATIC_DIR / "index.html"
            if index_file.exists():
                content = index_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self._send_json({"error": "index.html not found"}, 404)
            return

        if path == "/api/dashboard":
            self._send_json(get_full_dashboard_payload())
            return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/scan":
            res = execute_watchlist_scan(trigger_debates=True)
            self._send_json({"scan": res, "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/fast-forward":
            bars = int(body.get("bars", 60))
            res = run_historical_fast_forward(bars_to_replay=bars)
            execute_watchlist_scan(trigger_debates=True)
            self._send_json({"replay": res, "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/self-improve":
            res = run_self_improvement_cycle()
            self._send_json({"learning": res, "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/settings":
            if "watchlist_str" in body and isinstance(body["watchlist_str"], str):
                body["watchlist"] = [
                    s.strip().upper()
                    for s in body["watchlist_str"].split(",")
                    if s.strip()
                ]
            updated = save_settings(body)
            execute_watchlist_scan(trigger_debates=True)
            self._send_json({"settings": updated, "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/close-trade":
            t_id = int(body.get("trade_id", 0))
            res = manual_close_position(t_id)
            self._send_json({"result": res, "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/close-all-trades":
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT id FROM trades WHERE status = 'OPEN'")
            open_ids = [r["id"] for r in cur.fetchall()]
            conn.close()
            for tid in open_ids:
                manual_close_position(tid)
            self._send_json({"closed_count": len(open_ids), "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/toggle-bot":
            s = load_settings()
            new_state = bool(body.get("enabled", not s.get("auto_pilot_enabled", False)))
            updated = save_settings({"auto_pilot_enabled": new_state})
            if new_state:
                execute_watchlist_scan(trigger_debates=True)
            self._send_json({"auto_pilot_enabled": new_state, "settings": updated, "dashboard": get_full_dashboard_payload()})
            return

        if path == "/api/reset-account":
            res = reset_paper_account()
            execute_watchlist_scan(trigger_debates=True)
            self._send_json({"result": res, "dashboard": get_full_dashboard_payload()})
            return

        self._send_json({"error": "Unknown endpoint"}, 404)


def _prevent_windows_sleep() -> None:
    """
    Calls the native Windows Power Management API (SetThreadExecutionState)
    with ES_CONTINUOUS | ES_SYSTEM_REQUIRED (0x80000001) so Windows never
    suspends Python background threads overnight while the bot is running.
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        except Exception:
            pass


def _background_autopilot_worker() -> None:
    _prevent_windows_sleep()
    while True:
        time.sleep(10)
        try:
            _prevent_windows_sleep()
            settings = load_settings()
            if settings.get("auto_pilot_enabled"):
                execute_watchlist_scan(trigger_debates=True)
                time.sleep(max(15, int(settings.get("scan_interval_seconds", 300)) - 10))
        except Exception:
            pass


def bootstrap_initial_experience() -> None:
    """
    Initializes SQLite tables cleanly with $10,000.00 and ZERO synthetic dummy trades.
    Runs a live Bybit V5 scan and fetches live RSS crypto headlines.
    """
    init_db()
    execute_watchlist_scan(trigger_debates=True)


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    _prevent_windows_sleep()
    bootstrap_initial_experience()
    bg_thread = threading.Thread(target=_background_autopilot_worker, daemon=True)
    bg_thread.start()
    server = ThreadingHTTPServer((host, port), WhaleDashboardHandler)
    print(f"==================================================================")
    print(f" WHALE-MINDSET AUTONOMOUS TRADING BOT RUNNING AT:")
    print(f" -> http://{host}:{port}")
    print(f"==================================================================")
    server.serve_forever()


if __name__ == "__main__":
    port_num = int(os.getenv("PORT", "8080"))
    run_server(host="127.0.0.1", port=port_num)
