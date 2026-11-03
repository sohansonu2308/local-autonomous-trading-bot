# Whale-Mindset Autonomous Crypto Trading System

An institutional-grade, coin-agnostic **Market Microstructure + Multi-Agent Adversarial Debate + Quant-Gated Self-Improvement** crypto trading system with a realistic Paper Trading simulator and local Command Center Dashboard.

---

## Core Architecture

1. **Deterministic Market Microstructure Engine (`engine/quant_microstructure.py` & `engine/market_data.py`)**
   - Pulls public perpetual futures data from **Bybit V5** (with automatic fallback to a deterministic institutional microstructure feed when offline/sandboxed).
   - **100% Coin-Agnostic:** Every metric is normalized using ATR %, rolling Z-scores, and relative volume so the exact same mathematical logic applies to BTC, ETH, SOL, XRP, DOGE, SUI, or any custom USDT perpetual symbol you add.
   - **Built-in Local LiqTide Calculator:** Maps unmitigated retail leverage tiers (`10x`, `25x`, `50x`, `100x`) from high-volume entry nodes to locate nearest Upside Short Liquidation and Downside Long Liquidation magnet pools.
   - **Swing Failure Pattern (SFP) & CVD Absorption:** Detects stop-hunt sweeps and Taker Buy/Sell Cumulative Volume Delta divergences.

2. **3-Agent Adversarial Debate Committee (`engine/llm_agents.py`)**
   - **Agent 1 (Retail Bait Analyst):** Identifies what textbook retail traders are being baited into doing and where their stop-losses are clustered.
   - **Agent 2 (Whale & Market Maker Strategist):** Cites active rules from the local **Whale Playbook Memory** and targets the opposing liquidation magnet pool.
   - **Agent 3 (Chief Risk Officer / Judge):** Enforces portfolio heat limits, Regime Discipline, and minimum asymmetric Risk:Reward (`R:R`) before approving execution or vetoing the setup.
   - **Pluggable LLM API Key:** Supports **Gemini, OpenAI, Anthropic, OpenRouter, and Groq**. Paste your API key in `.env` or directly inside the Dashboard (`⚙️ LLM Key & Config`). Works out-of-the-box with Deterministic Quant Agents even before you paste your key.

3. **Realistic Paper Trading & Counterfactual Shadow Engine (`engine/paper_trader.py`)**
   - Starts with a **$10,000** virtual account.
   - Deducts realistic **Taker Fees (`0.055%`)** and **Volatility Slippage (`0.02%`)** on entries and exits.
   - Tracks **MAE (Maximum Adverse Excursion)** and **MFE (Maximum Favorable Excursion)** in `%` and `R`-multiples, automatically moving Stop-Loss to Breakeven once MFE reaches the active trigger threshold.
   - **Shadow Tracker:** Every setup vetoed by the Risk Officer is tracked counterfactually in parallel to audit whether the veto saved a `-1.0R` loss (`SMART_VETO_SAVED_LOSS`) or missed a winning trade (`MISSED_WINNER_TOO_STRICT`).

4. **Quant-Gated Self-Improvement Loop (`engine/self_improver.py`)**
   - Audits batches of closed paper trades (via MAE/MFE post-mortems) and resolved Shadow Trades.
   - Adjusts **Layer A (Mathematical Quant Weights)** and **Layer B (Whale Playbook Rules)** in SQLite (`data/whale_bot.db`).
   - **Backtest Gatekeeper:** Automatically rejects any proposed rule/parameter change that fails to improve historical Expectancy (`R`), preventing LLM overfitting to random market noise.

---

## Quick Start

### Option 1: Double-click Launcher
Double-click `start_bot.bat` in `D:\Zeldris\GEMINI_TRADING_BOT`.

### Option 2: Run from Terminal
```powershell
cd D:\Zeldris\GEMINI_TRADING_BOT
python app.py
```
Then open **http://127.0.0.1:8080** in your browser.
