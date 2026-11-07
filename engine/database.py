import sqlite3
import json
from datetime import datetime, timezone
from typing import Dict, Any, List
from config import DB_PATH, DEFAULT_QUANT_WEIGHTS, load_settings

INITIAL_PLAYBOOK_RULES = [
    {
        "rule_id": "WHALE-01",
        "category": "LIQUIDITY_SWEEP",
        "title": "Closed-Bar Swing Failure Pattern (SFP) + OI Flush Reversal",
        "condition": "Completed closed 15m/1h candle pierces a major swing high or low to trigger retail breakout buyers and stop-losses, then closes back inside the range while Open Interest drops or Funding is extreme.",
        "action": "Fade the false breakout toward the opposite internal liquidation pool. Place stop-loss 1.6x ATR beyond the sweep wick.",
        "confidence": 0.78,
        "sample_size": 14,
        "win_rate": 64.3,
        "origin": "CORE_DOCTRINE",
        "active": 1,
    },
    {
        "rule_id": "WHALE-02",
        "category": "CVD_ABSORPTION",
        "title": "Perp Aggressive Selling Absorbed by Limit Buyers",
        "condition": "Taker sell volume spikes heavily (negative CVD delta) at key support or downside liquidation cluster, but closed candle refuses to make a lower close (limit order absorption).",
        "action": "Enter LONG alongside passive whale accumulators once closed candle reclaims micro-structure high.",
        "confidence": 0.74,
        "sample_size": 11,
        "win_rate": 63.6,
        "origin": "CORE_DOCTRINE",
        "active": 1,
    },
    {
        "rule_id": "WHALE-03",
        "category": "CROWDED_FUNDING_SQUEEZE",
        "title": "Over-Leveraged Retail Funding Trap",
        "condition": "Funding rate Z-score exceeds +2.0 (crowded longs) or -2.0 (crowded shorts) combined with rising Open Interest into a stagnating price zone.",
        "action": "Do not join the retail crowd. Wait for a closed-bar liquidity sweep against the crowded side to ride the liquidation cascade.",
        "confidence": 0.81,
        "sample_size": 18,
        "win_rate": 66.7,
        "origin": "CORE_DOCTRINE",
        "active": 1,
    },
    {
        "rule_id": "WHALE-04",
        "category": "REGIME_DISCIPLINE",
        "title": "Never Fade High-Volume Spot Expansion",
        "condition": "When 1h/4h directional expansion has >2.2x relative volume and Open Interest is healthy without extreme funding divergence.",
        "action": "Veto counter-trend fade setups. Whales are driving trend continuation; only take pullback liquidity sweeps in the trend direction.",
        "confidence": 0.85,
        "sample_size": 20,
        "win_rate": 70.0,
        "origin": "CORE_DOCTRINE",
        "active": 1,
    },
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    settings = load_settings()
    initial_bal = float(settings.get("initial_balance_usd", 10000.0))
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            initial_balance REAL NOT NULL,
            cash_balance REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0.0,
            total_fees_paid REAL NOT NULL DEFAULT 0.0,
            peak_equity REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            equity REAL NOT NULL,
            cash_balance REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            drawdown_pct REAL NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            status TEXT NOT NULL,
            setup_type TEXT NOT NULL,
            regime TEXT NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL NOT NULL,
            exit_price REAL,
            stop_loss REAL NOT NULL,
            initial_stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            position_size_usd REAL NOT NULL,
            margin_used_usd REAL DEFAULT 0.0,
            qty REAL NOT NULL,
            leverage REAL NOT NULL,
            risk_usd REAL NOT NULL,
            rr_planned REAL NOT NULL,
            realized_pnl_usd REAL DEFAULT 0.0,
            realized_r REAL DEFAULT 0.0,
            unrealized_pnl_usd REAL DEFAULT 0.0,
            fees_paid_usd REAL DEFAULT 0.0,
            mae_pct REAL DEFAULT 0.0,
            mfe_pct REAL DEFAULT 0.0,
            mae_r REAL DEFAULT 0.0,
            mfe_r REAL DEFAULT 0.0,
            highest_price REAL NOT NULL,
            lowest_price REAL NOT NULL,
            bars_held INTEGER DEFAULT 0,
            max_hold_bars INTEGER DEFAULT 32,
            anomaly_score REAL DEFAULT 0.0,
            timemachine_verdict TEXT,
            retail_trap_thesis TEXT,
            whale_thesis TEXT,
            risk_judge_verdict TEXT,
            close_reason TEXT,
            thesis_grade TEXT,
            post_mortem_note TEXT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            is_historical_sim INTEGER DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_type TEXT NOT NULL,
            regime TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            highest_price REAL NOT NULL,
            lowest_price REAL NOT NULL,
            status TEXT NOT NULL,
            shadow_outcome TEXT,
            shadow_r REAL DEFAULT 0.0,
            veto_reason TEXT NOT NULL,
            anomaly_score REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_debates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            regime TEXT NOT NULL,
            anomaly_score REAL NOT NULL,
            setup_type TEXT NOT NULL,
            retail_agent_output TEXT NOT NULL,
            whale_agent_output TEXT NOT NULL,
            risk_judge_output TEXT NOT NULL,
            timemachine_summary TEXT,
            decision TEXT NOT NULL,
            llm_mode TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS playbook_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            condition TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL NOT NULL,
            sample_size INTEGER NOT NULL DEFAULT 1,
            win_rate REAL NOT NULL DEFAULT 50.0,
            origin TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_weights_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL,
            weights_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            backtest_expectancy_before REAL NOT NULL,
            backtest_expectancy_after REAL NOT NULL,
            approved INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_hash TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT,
            symbol_tag TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL
        )
        """
    )

    cur.execute("PRAGMA table_info(trades)")
    trade_cols = [r["name"] for r in cur.fetchall()]
    if "margin_used_usd" not in trade_cols:
        cur.execute("ALTER TABLE trades ADD COLUMN margin_used_usd REAL DEFAULT 0.0")

    now_iso = datetime.now(timezone.utc).isoformat()

    cur.execute("SELECT id FROM portfolio_state WHERE id = 1")
    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO portfolio_state (id, initial_balance, cash_balance, realized_pnl, total_fees_paid, peak_equity, updated_at)
            VALUES (1, ?, ?, 0.0, 0.0, ?, ?)
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

    cur.execute("SELECT COUNT(*) as cnt FROM playbook_rules")
    if cur.fetchone()["cnt"] == 0:
        for rule in INITIAL_PLAYBOOK_RULES:
            cur.execute(
                """
                INSERT INTO playbook_rules (rule_id, category, title, condition, action, confidence, sample_size, win_rate, origin, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule["rule_id"],
                    rule["category"],
                    rule["title"],
                    rule["condition"],
                    rule["action"],
                    rule["confidence"],
                    rule["sample_size"],
                    rule["win_rate"],
                    rule["origin"],
                    rule["active"],
                    now_iso,
                ),
            )

    cur.execute("SELECT COUNT(*) as cnt FROM quant_weights_history WHERE approved = 1")
    if cur.fetchone()["cnt"] == 0:
        cur.execute(
            """
            INSERT INTO quant_weights_history (version, weights_json, reason, backtest_expectancy_before, backtest_expectancy_after, approved, created_at)
            VALUES (1, ?, 'Initial Baseline Institutional Microstructure Weights', 0.35, 0.35, 1, ?)
            """,
            (json.dumps(DEFAULT_QUANT_WEIGHTS), now_iso),
        )

    conn.commit()
    conn.close()


def get_active_quant_weights() -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT weights_json FROM quant_weights_history WHERE approved = 1 ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["weights_json"])
        except Exception:
            pass
    return dict(DEFAULT_QUANT_WEIGHTS)


def get_active_playbook_rules() -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM playbook_rules WHERE active = 1 ORDER BY confidence DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
