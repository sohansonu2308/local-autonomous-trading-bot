# Indian Market (NSE Nifty 50 / BankNifty / F&O Stocks & MCX Commodities) Institutional Operator Bot

A local, self-improving autonomous trading bot & dashboard (`http://127.0.0.1:8090`) engineered for **Indian Equities, Indices (NSE Nifty 50, BankNifty), and MCX Commodities (Gold, Silver, Crude Oil, Natural Gas)**.

## Key Features
- **FII / Prop-Desk / Operator Microstructure Engine:**
  - Hunts retail stop-losses around **Previous Day High (`PDH`)**, **Previous Day Low (`PDL`)**, **Intraday Session VWAP**, and **30-Bar / 14-Bar Swing Failure Patterns (`SFP`)**.
  - Maps **Option Chain Call/Put OI Walls (`PCR`)** and round-strike short-covering / long-unwinding pools.
  - Built-in **Anti-Exhaustion Guard** (blocks shorting oversold capitulation `<38 RSI` and blocks buying overbought FOMO `>62 RSI` without a confirmed SFP sweep).
- **Pre-Trade Time Machine Simulator (`1,500` Block-Bootstrap Replays + Conditional Historical Setup Audit)**.
- **Realistic Indian Paper Trading (`₹10,00,000` / `₹10 Lakh` Starting Capital):**
  - `5.0x` MIS Intraday / SPAN+Exposure isolated margin (`30%` max `₹3,00,000` collateral per slot).
  - Deducts flat `₹20` brokerage + Indian statutory **STT/CTT + NSE/MCX Exchange Txn + SEBI + Stamp Duty + 18% GST** + `0.015%` slippage.
  - Enforces **NSE (`15:15 IST`)** and **MCX (`23:15 IST`)** Intraday Auto-Square-Off.
