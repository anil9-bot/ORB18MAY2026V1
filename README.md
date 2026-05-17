# ORB + VWAP Auto-Trading System
### Upstox API v2 · Flask · Nifty / BankNifty / Top NSE Stocks

---

## What this app does

A full-stack auto-trading web application that:

- **Runs the ORB + VWAP combined strategy** live during market hours
- **Places real orders** via the Upstox API (BUY/SELL at market)
- **Backtests on real historical OHLCV data** fetched directly from Upstox — no synthetic formulas
- Supports **Nifty, BankNifty, HDFC Bank, ICICI Bank, Reliance, Axis Bank, SBI, Kotak, Bajaj Finance, L&T, TCS, Infosys**
- Shows a **live signals feed, equity curve, full trade log, P&L metrics**

---

## File structure

```
trading_app/
├── app.py            ← Main Flask server (run this)
├── strategy.py       ← ORB + VWAP logic engine
├── backtest.py       ← Real-data backtesting engine
├── requirements.txt  ← Python dependencies
├── start.sh          ← Mac startup script (one-click)
└── templates/
    └── index.html    ← Full dark-themed trading UI
```

---

## One-time setup

### Step 1 — Get Upstox API credentials

1. Go to **https://developer.upstox.com** → Login with your Upstox account
2. Click **Create App**
3. Fill in any app name (e.g. "My ORB Trader")
4. Set **Redirect URL** exactly as: `http://localhost:8080/callback`
5. Copy your **API Key** (= Client ID) and **Secret Key** (= Client Secret)

### Step 2 — Put credentials in app.py

Open `app.py` in any text editor and edit lines 24–25:

```python
CLIENT_ID     = 'paste_your_api_key_here'
CLIENT_SECRET = 'paste_your_secret_key_here'
```

OR use environment variables (safer — keeps secrets out of code):

```bash
export UPSTOX_CLIENT_ID=your_api_key
export UPSTOX_CLIENT_SECRET=your_secret_key
```

### Step 3 — Install Python dependencies

```bash
cd trading_app
pip3 install -r requirements.txt
```

---

## Running the app (every morning)

### Easiest way (double-click or terminal):
```bash
cd trading_app
bash start.sh
```

### Manual way:
```bash
cd trading_app
python3 app.py
```

Then open **http://localhost:8080** in your browser.

> **Important:** Upstox access tokens expire every day at midnight.
> You must click **Login with Upstox** each morning before 9:15 AM.

---

## How to use

### Live Trading

1. Open http://localhost:8080
2. Click **Login with Upstox** (top right) → authorise
3. Go to **Dashboard** tab
4. Select instruments to trade (e.g. NIFTY + BANKNIFTY)
5. Set your lots, capital, risk %
6. Click **▶ Start Strategy**

The app will now:
- Monitor the first 15-min candle (Opening Range)
- Watch for ORB breakout confirmed by VWAP
- Place a market order via Upstox API
- Manage T1 (50% exit at 1.5R) and T2 (50% exit at 2.5R)
- Force-exit any open position at 3:15 PM

### Backtesting

1. Go to **Backtest** tab
2. Select instrument, date range, lots, OR window
3. Click **▶ Run Backtest**

The engine fetches real candle data from Upstox and simulates trades day by day.
Results include: Win Rate, Total P&L, Return %, Profit Factor, Sharpe Ratio,
Max Drawdown, Equity Curve chart, and full trade-by-trade log.

> **Note:** Upstox historical API provides up to 1 year of intraday data.
> For indices, P&L is shown in futures-equivalent (underlying price × lot size).
> Actual options P&L will differ due to premium, IV, and time decay.

---

## Strategy Logic

### Entry Rules (LONG)
- 5-min candle **closes ABOVE** the Opening Range High
- Close price is **above VWAP** (institutional bias = bullish)
- Volume ≥ 75% of session average (momentum confirmation)
- Only after Opening Range is complete (9:30 AM for 15-min OR)
- No new entries after 2:00 PM

### Entry Rules (SHORT)
- Mirror of LONG — close below OR Low AND below VWAP

### Exit Rules
| Exit Type | Trigger | Action |
|-----------|---------|--------|
| Stop Loss | Price hits OR Low (LONG) or OR High (SHORT) | Exit 100% |
| Target 1  | Entry ± 1.5 × risk | Exit 50%, move SL to entry |
| Target 2  | Entry ± 2.5 × risk | Exit remaining 50% |
| Time Exit | 3:15 PM | Force-exit 100% at market |

### Position Sizing
```
Max Risk = Capital × Risk% / 100
Lots     = floor(Max Risk / (Risk_per_lot))
         = floor(Max Risk / (Stop_Distance × Lot_Size))
Capped at 10 lots maximum
```

---

## Expiry rules used

| Instrument | Expiry Type |
|------------|-------------|
| NIFTY      | Weekly — next Tuesday |
| BANKNIFTY  | Monthly — last Tuesday of month |
| Stocks     | Monthly — last Thursday of month |

---

## Important disclaimers

- Always verify orders on your **Upstox account/app** independently
- This system places **MARKET orders** — subject to slippage during high volatility
- Backtest P&L is on **underlying futures-equivalent**, not options premium
- Do NOT run on **expiry days, budget day, RBI policy days** without reducing size
- Past backtest results do not guarantee future performance
- **Never risk more than you can afford to lose**

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Port 8080 already in use | `lsof -i :8080` then `kill -9 <PID>` |
| Login fails / no token | Verify Redirect URI = `http://localhost:8080/callback` exactly |
| Backtest returns no data | Token may have expired — re-login |
| "No candles" on backtest | Date range may be beyond 1 year, or market holiday range |
| App won't start | Run `pip3 install -r requirements.txt` again |

---

*Built with Python 3.12 · Flask 3 · Upstox API v2 · Chart.js 4*
