"""
Auto-Trading Web App — ORB + VWAP Strategy
===========================================
Upstox API v2  |  Flask backend  |  Port 8080

Setup:
  1. pip install -r requirements.txt
  2. Set CLIENT_ID and CLIENT_SECRET below (from Upstox developer console)
     Redirect URI in Upstox console must be: http://localhost:8080/callback
  3. python app.py
  4. Open http://localhost:8080
  5. Click Login → authorise → start trading or backtesting
"""

import os, json, time, threading, urllib.parse, calendar
import requests
from datetime import datetime, date, timedelta
from flask import (Flask, request, redirect, session,
                   jsonify, render_template, url_for)
from strategy  import ORBVWAPStrategy
from backtest  import BacktestEngine

# ─────────────────────────────────────────────
#  CONFIGURATION  ← Edit these two lines
# ─────────────────────────────────────────────
CLIENT_ID     = os.getenv('UPSTOX_CLIENT_ID',     'f98dc62e-dddf-4278-8c77-171ab144d74f')
CLIENT_SECRET = os.getenv('UPSTOX_CLIENT_SECRET', 'kx9317xvn3')
REDIRECT_URI  = 'http://localhost:8080/callback'
PORT          = 8080
# ─────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(24)

UPSTOX_BASE    = 'https://api.upstox.com/v2'
AUTH_URL       = 'https://api.upstox.com/v2/login/authorization/dialog'
TOKEN_URL      = 'https://api.upstox.com/v2/login/authorization/token'

# ──────────────────────────────────────────────────────────────────────
#  Instrument Master
# ──────────────────────────────────────────────────────────────────────
INSTRUMENTS = {
    # Indices — traded via ATM options (CE for LONG, PE for SHORT)
    'NIFTY': {
        'key': 'NSE_INDEX|Nifty 50', 'lot_size': 75,
        'strike_gap': 50,  'nse_symbol': 'NIFTY', 'iv': 0.16,
        'type': 'index',   'expiry': 'weekly_tuesday',
        'label': 'Nifty 50', 'suitable': 5,
        'desc': 'Weekly Tuesday expiry. Highest liquidity. Best for ORB.'
    },
    'BANKNIFTY': {
        'key': 'NSE_INDEX|Nifty Bank', 'lot_size': 30,
        'strike_gap': 100, 'nse_symbol': 'BANKNIFTY', 'iv': 0.2,
        'type': 'index',   'expiry': 'monthly_last_tuesday',
        'label': 'Bank Nifty', 'suitable': 5,
        'desc': 'Monthly last Tuesday expiry. Volatile. Best intraday swings.'
    },
    # Top NSE F&O stocks
    'RELIANCE': {
        'key': 'NSE_EQ|INE002A01018', 'lot_size': 250,
        'strike_gap': 20,  'nse_symbol': 'RELIANCE', 'iv': 0.23,
        'type': 'stock',   'label': 'Reliance Industries', 'suitable': 4,
        'desc': 'High ADR, strong trend days. Energy sector leader.'
    },
    'HDFCBANK': {
        'key': 'NSE_EQ|INE040A01034', 'lot_size': 550,
        'strike_gap': 10,  'nse_symbol': 'HDFCBANK', 'iv': 0.22,
        'type': 'stock',   'label': 'HDFC Bank', 'suitable': 5,
        'desc': 'Banking heavyweight. Excellent volume & range for ORB.'
    },
    'ICICIBANK': {
        'key': 'NSE_EQ|INE090A01021', 'lot_size': 700,
        'strike_gap': 5,   'nse_symbol': 'ICICIBANK', 'iv': 0.24,
        'type': 'stock',   'label': 'ICICI Bank', 'suitable': 5,
        'desc': 'High beta banking stock. Strong momentum breakouts.'
    },
    'TCS': {
        'key': 'NSE_EQ|INE467B01029', 'lot_size': 150,
        'strike_gap': 50,  'nse_symbol': 'TCS', 'iv': 0.2,
        'type': 'stock',   'label': 'TCS', 'suitable': 3,
        'desc': 'Large cap IT. Lower volatility — conservative ORB.'
    },
    'INFY': {
        'key': 'NSE_EQ|INE009A01021', 'lot_size': 300,
        'strike_gap': 20,  'nse_symbol': 'INFY', 'iv': 0.21,
        'type': 'stock',   'label': 'Infosys', 'suitable': 3,
        'desc': 'IT sector. Clean trends on result & guidance days.'
    },
    'AXISBANK': {
        'key': 'NSE_EQ|INE238A01034', 'lot_size': 1200,
        'strike_gap': 5,   'nse_symbol': 'AXISBANK', 'iv': 0.25,
        'type': 'stock',   'label': 'Axis Bank', 'suitable': 4,
        'desc': 'High liquidity banking stock. Good ORB follow-through.'
    },
    'SBIN': {
        'key': 'NSE_EQ|INE062A01020', 'lot_size': 1500,
        'strike_gap': 5,   'nse_symbol': 'SBIN', 'iv': 0.25,
        'type': 'stock',   'label': 'SBI', 'suitable': 4,
        'desc': 'PSU giant. Big gap-ups on macro/budget news.'
    },
    'BAJFINANCE': {
        'key': 'NSE_EQ|INE296A01024', 'lot_size': 125,
        'strike_gap': 50,  'nse_symbol': 'BAJFINANCE', 'iv': 0.27,
        'type': 'stock',   'label': 'Bajaj Finance', 'suitable': 4,
        'desc': 'NBFC heavyweight. Sharp moves on rate/credit data.'
    },
    'WIPRO': {
        'key': 'NSE_EQ|INE075A01022', 'lot_size': 1500,
        'strike_gap': 5,   'nse_symbol': 'WIPRO', 'iv': 0.22,
        'type': 'stock',   'label': 'Wipro', 'suitable': 3,
        'desc': 'IT mid-cap. Lower daily range.'
    },
    'HCLTECH': {
        'key': 'NSE_EQ|INE860A01027', 'lot_size': 350,
        'strike_gap': 20,  'nse_symbol': 'HCLTECH', 'iv': 0.21,
        'type': 'stock',   'label': 'HCL Technologies', 'suitable': 3,
        'desc': 'IT sector. Decent trend follow-through.'
    },
    'KOTAKBANK': {
        'key': 'NSE_EQ|INE237A01028', 'lot_size': 400,
        'strike_gap': 20,  'nse_symbol': 'KOTAKBANK', 'iv': 0.22,
        'type': 'stock',   'label': 'Kotak Mahindra Bank', 'suitable': 4,
        'desc': 'Premium private bank. Institutional levels respected well.'
    },
    'LT': {
        'key': 'NSE_EQ|INE018A01030', 'lot_size': 150,
        'strike_gap': 20,  'nse_symbol': 'LT', 'iv': 0.23,
        'type': 'stock',   'label': 'Larsen & Toubro', 'suitable': 4,
        'desc': 'Infra & engineering. Strong on capex/order news.'
    },
}

# ──────────────────────────────────────────────────────────────────────
#  In-memory live strategy state
# ──────────────────────────────────────────────────────────────────────
_state_lock  = threading.Lock()
live_state   = {
    'running':      False,
    'instruments':  [],
    'config':       {},
    'positions':    {},    # instrument → position info
    'signals':      [],    # last 50 signals
    'trades_today': [],    # completed trades today
    'pnl_today':    0.0,
    'threads':      {}     # instrument → thread
}

def _log_signal(instrument, signal):
    with _state_lock:
        live_state['signals'].insert(0, {
            **signal,
            'instrument': instrument,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })
        live_state['signals'] = live_state['signals'][:50]

# ──────────────────────────────────────────────────────────────────────
#  Upstox API helpers
# ──────────────────────────────────────────────────────────────────────
def _headers(token=None):
    t = token or session.get('access_token')
    return {'Authorization': f'Bearer {t}', 'Accept': 'application/json'}

def _get(url, token=None, params=None):
    try:
        r = requests.get(url, headers=_headers(token), params=params, timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

def get_ltp(instrument_key: str, token: str) -> float:
    enc = urllib.parse.quote(instrument_key, safe='')
    data = _get(f"{UPSTOX_BASE}/market-quote/ltp?instrument_key={enc}", token)
    try:
        for v in data.get('data', {}).values():
            return float(v.get('last_price', 0))
    except Exception:
        return 0.0

def get_profile(token: str) -> dict:
    return _get(f"{UPSTOX_BASE}/user/profile", token).get('data', {})

def get_funds(token: str) -> dict:
    """
    Fetch funds from Upstox.
    Checks every known field name across all Upstox v2 response shapes.

    Why ₹0 shows even with a balance:
      Upstox splits balance across multiple fields:
        available_margin  = funds free to trade right now
        payin_amount      = deposits pending settlement (T+1)
        notional_cash     = cash equivalent of holdings
      If you deposited ₹12 recently it may sit in payin_amount, not available_margin.
    We show the largest non-zero value found.
    """
    raw_resp = _get(f"{UPSTOX_BASE}/user/get-funds-and-margin", token)

    data   = raw_resp.get('data') or raw_resp
    equity = {}
    if isinstance(data, dict):
        equity = (data.get('equity') or data.get('Equity') or
                  data.get('EQ')     or data.get('eq')     or data)

    def _flt(val):
        try:    return float(val or 0)
        except: return 0.0

    # Extract every meaningful field
    available  = _flt(equity.get('available_margin') or equity.get('net')
                      or equity.get('available')      or equity.get('availableBalance'))
    used       = _flt(equity.get('used_margin') or equity.get('utilised'))
    payin      = _flt(equity.get('payin_amount') or equity.get('payinAmount'))
    notional   = _flt(equity.get('notional_cash'))
    adhoc      = _flt(equity.get('adhoc_margin'))
    collateral = _flt(equity.get('collateral'))

    # "Total usable" = everything that could be available to trade
    total_usable = available + payin + notional + adhoc + collateral

    # Display balance: use available_margin if >0, else fall back to
    # any other non-zero field so a ₹12 payin shows correctly
    if available > 0:
        display = available
    elif total_usable > 0:
        display = total_usable
    else:
        # Last resort: sum every numeric field
        display = sum(_flt(v) for v in equity.values()
                      if isinstance(v, (int, float, str)))

    return {
        'available_margin': round(available,  2),
        'used_margin':      round(used,        2),
        'payin_amount':     round(payin,        2),
        'total_balance':    round(total_usable, 2),
        'display_balance':  round(display,      2),   # ← always the best value to show
        'raw':              raw_resp
    }

def get_positions(token: str) -> list:
    return _get(f"{UPSTOX_BASE}/portfolio/short-term-positions", token).get('data', [])

def get_orders(token: str) -> list:
    return _get(f"{UPSTOX_BASE}/order/retrieve-all", token).get('data', [])

def get_option_chain(instrument_key: str, expiry_date: str, token: str) -> list:
    enc = urllib.parse.quote(instrument_key, safe='')
    data = _get(f"{UPSTOX_BASE}/option/chain?instrument_key={enc}&expiry_date={expiry_date}", token)
    return data.get('data', [])

def place_order(instrument_token: str, qty: int, txn_type: str, token: str) -> dict:
    url  = f"{UPSTOX_BASE}/order/place"
    body = {
        'quantity':           qty,
        'product':            'I',          # Intraday (MIS)
        'validity':           'DAY',
        'price':              0,
        'tag':                'ORB_VWAP',
        'instrument_token':   instrument_token,
        'order_type':         'MARKET',
        'transaction_type':   txn_type,     # 'BUY' or 'SELL'
        'disclosed_quantity': 0,
        'trigger_price':      0,
        'is_amo':             False
    }
    try:
        r = requests.post(url, headers={**_headers(token),
                          'Content-Type': 'application/json'},
                          json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def get_atm_option_key(index_key: str, direction: str, strike_gap: int,
                       expiry_date: str, token: str):
    """
    Returns (option_instrument_key, ltp) for ATM CE (LONG) or PE (SHORT).
    """
    ltp = get_ltp(index_key, token)
    if ltp <= 0:
        return None, 0

    atm = round(ltp / strike_gap) * strike_gap
    opt_type = 'CE' if direction == 'LONG' else 'PE'

    chain = get_option_chain(index_key, expiry_date, token)
    for item in chain:
        if (abs(item.get('strike_price', 0) - atm) < 1 and
                item.get('option_type') == opt_type):
            return item.get('instrument_key'), item.get('market_data', {}).get('ltp', 0)

    # Fallback: find nearest strike
    best, best_key = None, None
    for item in chain:
        if item.get('option_type') != opt_type:
            continue
        diff = abs(item.get('strike_price', 0) - ltp)
        if best is None or diff < best:
            best     = diff
            best_key = item.get('instrument_key')
    return best_key, 0

# ──────────────────────────────────────────────────────────────────────
#  Expiry calculator
# ──────────────────────────────────────────────────────────────────────
def next_expiry(instrument_name: str) -> str:
    """
    NIFTY     → next weekly Tuesday
    BANKNIFTY → last Tuesday of current (or next) month
    Stocks    → next weekly Thursday (monthly F&O)
    """
    today = date.today()

    if 'BANK' in instrument_name.upper():
        # Monthly: last Tuesday of month
        year, month = today.year, today.month
        for _ in range(2):   # try this month, then next
            cal      = calendar.monthcalendar(year, month)
            tuesdays = [week[1] for week in cal if week[1] != 0]
            exp      = date(year, month, tuesdays[-1])
            if exp >= today:
                return exp.strftime('%Y-%m-%d')
            if month == 12: year, month = year+1, 1
            else:           month += 1
        return exp.strftime('%Y-%m-%d')

    elif 'NIFTY' in instrument_name.upper():
        # Weekly Tuesday
        days = (1 - today.weekday()) % 7   # Tuesday = weekday 1
        if days == 0:
            days = 7
        return (today + timedelta(days=days)).strftime('%Y-%m-%d')

    else:
        # Stock options — monthly last Thursday
        year, month = today.year, today.month
        for _ in range(2):
            cal       = calendar.monthcalendar(year, month)
            thursdays = [week[3] for week in cal if week[3] != 0]
            exp       = date(year, month, thursdays[-1])
            if exp >= today:
                return exp.strftime('%Y-%m-%d')
            if month == 12: year, month = year+1, 1
            else:           month += 1
        return exp.strftime('%Y-%m-%d')

# ──────────────────────────────────────────────────────────────────────
#  Live Strategy Thread
# ──────────────────────────────────────────────────────────────────────
def _strategy_worker(instrument_name: str, instr: dict, cfg: dict, token: str):
    """Background thread: runs ORB+VWAP for one instrument in real-time."""
    be       = BacktestEngine(token)
    strategy = ORBVWAPStrategy(
        instrument   = instrument_name,
        lot_size     = instr['lot_size'],
        capital      = cfg.get('capital', 400_000),
        risk_pct     = cfg.get('risk_pct', 1.0),
        or_minutes   = cfg.get('or_minutes', 15),
        t1_ratio     = cfg.get('t1_ratio', 1.5),
        t2_ratio     = cfg.get('t2_ratio', 2.5)
    )

    lots         = cfg.get('lots', 1)
    expiry       = next_expiry(instrument_name)
    prev_candle  = None
    option_key   = None    # The option instrument_key when in a position
    session_date = date.today()

    print(f"[{instrument_name}] Strategy thread started. Expiry: {expiry}")

    while live_state['running'] and instrument_name in live_state['instruments']:
        try:
            now = datetime.now()

            # Reset at start of new session
            if date.today() != session_date:
                strategy.reset_session()
                session_date = date.today()
                expiry       = next_expiry(instrument_name)
                option_key   = None
                prev_candle  = None
                print(f"[{instrument_name}] New session. Expiry: {expiry}")

            # Only trade during market hours
            mkt_open  = now.replace(hour=9, minute=15, second=0, microsecond=0)
            mkt_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
            force_exit_time = now.replace(hour=15, minute=15, second=0, microsecond=0)

            if now < mkt_open:
                time.sleep(30)
                continue
            if now > mkt_close:
                print(f"[{instrument_name}] Market closed. Thread exiting.")
                break

            # Force-exit at 3:15 PM
            if now >= force_exit_time and strategy.position:
                ltp = get_ltp(instr['key'], token)
                res = strategy.force_exit(ltp)
                if res:
                    _log_signal(instrument_name, res)
                    if option_key and instr['type'] == 'index':
                        txn = 'SELL' if strategy.position == 'LONG' else 'BUY'
                        place_order(option_key, lots * instr['lot_size'], txn, token)
                    option_key = None
                    with _state_lock:
                        live_state['pnl_today'] += res.get('pnl', 0)
                        live_state['positions'].pop(instrument_name, None)
                time.sleep(60)
                continue

            # Fetch latest intraday candles
            candles = be.fetch_intraday(instr['key'])
            if not candles or len(candles) < 2:
                time.sleep(30)
                continue

            # Use last completed candle (index -2; -1 is still forming)
            last = candles[-2]
            if prev_candle and last['time'] == prev_candle['time']:
                time.sleep(20)
                continue

            prev_candle = last
            res         = strategy.process_candle(last)

            if not res:
                time.sleep(20)
                continue

            _log_signal(instrument_name, res)
            action = res.get('action')

            # ── Entry ──
            if action in ('BUY', 'SELL') and not option_key:
                if instr['type'] == 'index':
                    direction   = res['direction']
                    okey, oltp  = get_atm_option_key(
                        instr['key'], direction,
                        instr['strike_gap'], expiry, token
                    )
                    if okey:
                        txn        = 'BUY'
                        order_resp = place_order(okey, lots * instr['lot_size'], txn, token)
                        option_key = okey
                        print(f"[{instrument_name}] {action} order placed. "
                              f"Option: {okey}. Response: {order_resp}")
                    else:
                        print(f"[{instrument_name}] Could not find ATM option key.")
                else:
                    # Stock — buy/sell equity directly
                    txn        = 'BUY' if action == 'BUY' else 'SELL'
                    qty        = lots * instr['lot_size']
                    order_resp = place_order(instr['key'], qty, txn, token)
                    option_key = instr['key']
                    print(f"[{instrument_name}] {action} stock order. Response: {order_resp}")

                with _state_lock:
                    live_state['positions'][instrument_name] = {
                        'direction':   res.get('direction'),
                        'entry':       res.get('entry'),
                        'sl':          res.get('stop_loss'),
                        't1':          res.get('target1'),
                        't2':          res.get('target2'),
                        'lots':        lots,
                        'option_key':  option_key,
                        'entry_time':  now.strftime('%H:%M'),
                        'pnl':         0
                    }

            # ── Exit ──
            elif action in ('EXIT_SL', 'EXIT_T1', 'EXIT_T2', 'EXIT_TIME') and option_key:
                if action != 'EXIT_T1':   # Full exit
                    txn  = 'SELL' if (live_state['positions']
                                      .get(instrument_name, {})
                                      .get('direction') == 'LONG') else 'BUY'
                    place_order(option_key, lots * instr['lot_size'], txn, token)
                    option_key = None
                    with _state_lock:
                        live_state['pnl_today']  += res.get('pnl', 0)
                        live_state['trades_today'].append({
                            'instrument': instrument_name,
                            'time':       now.strftime('%H:%M'),
                            **res
                        })
                        live_state['positions'].pop(instrument_name, None)
                else:
                    # T1 partial — sell half
                    half_qty = (lots * instr['lot_size']) // 2
                    txn      = 'SELL' if (live_state['positions']
                                         .get(instrument_name, {})
                                         .get('direction') == 'LONG') else 'BUY'
                    place_order(option_key, half_qty, txn, token)
                    with _state_lock:
                        live_state['pnl_today'] += res.get('pnl', 0)
                        if instrument_name in live_state['positions']:
                            live_state['positions'][instrument_name]['sl'] = res.get('entry', 0)

            # ── Update unrealised PnL ──
            elif action == 'HOLD' and instrument_name in live_state['positions']:
                with _state_lock:
                    live_state['positions'][instrument_name]['pnl'] = res.get('unrealized', 0)

            time.sleep(15)   # Poll every 15 seconds

        except Exception as e:
            print(f"[{instrument_name}] Error: {e}")
            time.sleep(30)

    with _state_lock:
        live_state['threads'].pop(instrument_name, None)
    print(f"[{instrument_name}] Thread stopped.")

# ──────────────────────────────────────────────────────────────────────
#  Flask Routes
# ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    logged_in = bool(session.get('access_token'))
    return render_template('index.html', logged_in=logged_in)


@app.route('/login')
def login():
    params = {
        'response_type': 'code',
        'client_id':     CLIENT_ID,
        'redirect_uri':  REDIRECT_URI
    }
    return redirect(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return redirect('/?error=no_code')
    try:
        resp = requests.post(TOKEN_URL, data={
            'code':          code,
            'client_id':     CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'redirect_uri':  REDIRECT_URI,
            'grant_type':    'authorization_code'
        }, headers={'Accept': 'application/json'})
        data = resp.json()
        token = data.get('access_token')
        if not token:
            return redirect('/?error=no_token')
        session['access_token']  = token
        session['token_type']    = data.get('token_type', 'Bearer')
        profile = get_profile(token)
        session['user_name']  = profile.get('user_name', 'Trader')
        session['user_email'] = profile.get('email', '')
    except Exception as e:
        return redirect(f'/?error={str(e)}')
    return redirect('/')


@app.route('/logout')
def logout():
    # Stop all strategy threads first
    with _state_lock:
        live_state['running']     = False
        live_state['instruments'] = []
    session.clear()
    return redirect('/')


# ── API: status & data ──

@app.route('/api/status')
def api_status():
    token = session.get('access_token')
    # Fetch funds inline so the dashboard refreshes every poll cycle
    funds_data = get_funds(token) if token else {}
    with _state_lock:
        return jsonify({
            'logged_in':        bool(token),
            'user_name':        session.get('user_name', ''),
            'running':          live_state['running'],
            'instruments':      live_state['instruments'],
            'positions':        live_state['positions'],
            'signals':          live_state['signals'][:20],
            'trades_today':     live_state['trades_today'][-20:],
            'pnl_today':        round(live_state['pnl_today'], 2),
            # Funds — use display_balance (best non-zero value across all fields)
            'available_margin': funds_data.get('display_balance', 0),
            'used_margin':      funds_data.get('used_margin',     0),
            'total_balance':    funds_data.get('total_balance',   0),
            'payin_amount':     funds_data.get('payin_amount',    0),
        })


@app.route('/api/profile')
def api_profile():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401
    return jsonify(get_profile(token))


@app.route('/api/funds')
def api_funds():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in',
                        'available_margin': 0, 'used_margin': 0,
                        'total_balance': 0}), 401
    return jsonify(get_funds(token))


@app.route('/api/debug/funds-raw')
def api_debug_funds():
    """
    Returns the raw Upstox API response for funds.
    Use this from the browser: http://localhost:8080/api/debug/funds-raw
    to diagnose why Available Funds shows ₹0.
    """
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401
    raw = _get(f"{UPSTOX_BASE}/user/get-funds-and-margin", token)
    parsed = get_funds(token)
    return jsonify({
        'raw_upstox_response': raw,
        'parsed_available':    parsed.get('available_margin'),
        'parsed_used':         parsed.get('used_margin'),
        'parsed_total':        parsed.get('total_balance'),
    })

@app.route('/api/positions')
def api_positions():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401
    return jsonify({'positions': get_positions(token)})


@app.route('/api/orders')
def api_orders():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401
    return jsonify({'orders': get_orders(token)})


@app.route('/api/instruments')
def api_instruments():
    return jsonify({'instruments': {
        k: {kk: vv for kk, vv in v.items() if kk != 'key'}
        for k, v in INSTRUMENTS.items()
    }})


@app.route('/api/intraday')
def api_intraday():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401
    instrument = request.args.get('instrument', 'NIFTY')
    instr = INSTRUMENTS.get(instrument)
    if not instr:
        return jsonify({'error': 'Unknown instrument'}), 400
    be = BacktestEngine(token)
    candles = be.fetch_intraday(instr['key'])
    return jsonify({'candles': [
        {**c, 'time': c['time'].strftime('%H:%M')} for c in candles
    ]})


# ── API: strategy control ──

@app.route('/api/strategy/start', methods=['POST'])
def api_start():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401

    body        = request.get_json() or {}
    instruments = body.get('instruments', ['NIFTY'])
    cfg         = {
        'capital':    float(body.get('capital',    400_000)),
        'risk_pct':   float(body.get('risk_pct',   1.0)),
        'or_minutes': int(body.get('or_minutes',   15)),
        'lots':       int(body.get('lots',          1)),
        't1_ratio':   float(body.get('t1_ratio',   1.5)),
        't2_ratio':   float(body.get('t2_ratio',   2.5)),
    }

    # Validate instruments
    valid = [i for i in instruments if i in INSTRUMENTS]
    if not valid:
        return jsonify({'error': 'No valid instruments selected'}), 400

    with _state_lock:
        live_state['running']     = True
        live_state['config']      = cfg
        live_state['pnl_today']   = 0.0
        live_state['trades_today']= []
        live_state['signals']     = []
        live_state['positions']   = {}

        for name in valid:
            if name in live_state['instruments']:
                continue   # already running
            live_state['instruments'].append(name)
            t = threading.Thread(
                target=_strategy_worker,
                args=(name, INSTRUMENTS[name], cfg, token),
                daemon=True, name=f"strat_{name}"
            )
            t.start()
            live_state['threads'][name] = t

    return jsonify({'status': 'started', 'instruments': valid, 'config': cfg})


@app.route('/api/strategy/stop', methods=['POST'])
def api_stop():
    body       = request.get_json() or {}
    instrument = body.get('instrument', 'ALL')

    with _state_lock:
        if instrument == 'ALL':
            live_state['running']     = False
            live_state['instruments'] = []
        elif instrument in live_state['instruments']:
            live_state['instruments'].remove(instrument)
            if not live_state['instruments']:
                live_state['running'] = False

    return jsonify({'status': 'stopped', 'instrument': instrument})


# ── API: backtest ──

@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in — backtest requires Upstox historical data access'}), 401

    body       = request.get_json() or {}
    instrument = body.get('instrument', 'NIFTY')
    instr      = INSTRUMENTS.get(instrument)
    if not instr:
        return jsonify({'error': 'Unknown instrument'}), 400

    from_date  = body.get('from_date')
    to_date    = body.get('to_date')
    if not from_date or not to_date:
        return jsonify({'error': 'from_date and to_date are required (YYYY-MM-DD)'}), 400

    try:
        be = BacktestEngine(token)
        results = be.run(
            instrument_key  = instr['key'],
            instrument_name = instr['label'],
            lot_size        = instr['lot_size'],
            strike_gap      = instr.get('strike_gap', 50),
            nse_symbol      = instr.get('nse_symbol', instrument),
            from_date       = from_date,
            to_date         = to_date,
            capital         = float(body.get('capital',    400_000)),
            risk_pct        = float(body.get('risk_pct',   1.0)),
            or_minutes      = int(body.get('or_minutes',   15)),
            lots            = int(body.get('lots',          1)),
            t1_ratio        = float(body.get('t1_ratio',   1.5)),
            t2_ratio        = float(body.get('t2_ratio',   2.5)),
            iv              = float(instr.get('iv', 0.20))
        )
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── API: month-wise backtest ──

@app.route('/api/backtest/monthly', methods=['POST'])
def api_backtest_monthly():
    """
    Runs the full backtest once, then groups trades + equity by calendar month.
    Returns per-month stats + overall summary — no extra API calls needed.
    """
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401

    body       = request.get_json() or {}
    instrument = body.get('instrument', 'BANKNIFTY')
    instr      = INSTRUMENTS.get(instrument)
    if not instr:
        return jsonify({'error': 'Unknown instrument'}), 400

    from_date = body.get('from_date')
    to_date   = body.get('to_date')
    if not from_date or not to_date:
        return jsonify({'error': 'from_date and to_date are required'}), 400

    try:
        be = BacktestEngine(token)

        # Single full backtest run ─ no repeated API calls
        full = be.run(
            instrument_key  = instr['key'],
            instrument_name = instr['label'],
            lot_size        = instr['lot_size'],
            strike_gap      = instr.get('strike_gap', 100),
            nse_symbol      = instr.get('nse_symbol', instrument),
            from_date       = from_date,
            to_date         = to_date,
            capital         = float(body.get('capital',    400_000)),
            risk_pct        = float(body.get('risk_pct',   1.0)),
            or_minutes      = int(body.get('or_minutes',   15)),
            lots            = int(body.get('lots',          1)),
            t1_ratio        = float(body.get('t1_ratio',   1.5)),
            t2_ratio        = float(body.get('t2_ratio',   2.5)),
            iv              = float(instr.get('iv', 0.20))
        )

        if 'error' in full:
            return jsonify(full), 500

        cap = float(body.get('capital', 400_000))

        # ── Group trades by YYYY-MM ──
        by_month: dict = {}
        for t in full.get('trades', []):
            mk = t.get('date', '')[:7]   # 'YYYY-MM'
            by_month.setdefault(mk, []).append(t)

        # ── Group equity curve by YYYY-MM ──
        eq_by_month: dict = {}
        for e in full.get('equity_curve', []):
            mk = e.get('date', '')[:7]
            eq_by_month.setdefault(mk, []).append(e)

        # ── Build monthly summary rows ──
        monthly = []
        for mk in sorted(by_month.keys()):
            trades = by_month[mk]
            wins   = [t for t in trades if t.get('result') == 'WIN']
            losses = [t for t in trades if t.get('result') == 'LOSS']
            longs  = [t for t in trades if t.get('direction') == 'LONG']
            shorts = [t for t in trades if t.get('direction') == 'SHORT']

            pnl = sum(float(t.get('pnl') or 0) for t in trades)

            # Best / worst trade
            pnls = [float(t.get('pnl') or 0) for t in trades]
            best  = max(pnls) if pnls else 0
            worst = min(pnls) if pnls else 0

            monthly.append({
                'month':        datetime.strptime(mk, '%Y-%m').strftime('%b %Y'),
                'month_key':    mk,
                'total_trades': len(trades),
                'wins':         len(wins),
                'losses':       len(losses),
                'long_trades':  len(longs),
                'short_trades': len(shorts),
                'win_rate':     round(len(wins) / len(trades) * 100, 1) if trades else 0,
                'total_pnl':    round(pnl, 2),
                'return_pct':   round(pnl / cap * 100, 2),
                'best_trade':   round(best, 2),
                'worst_trade':  round(worst, 2),
                'result':       'PROFIT' if pnl > 0 else 'LOSS'
            })

        return jsonify({
            'monthly':  monthly,
            'overall':  full['summary'],
            'equity_curve': full.get('equity_curve', [])
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/debug/test-connection', methods=['POST'])
def api_test_connection():
    """
    Diagnostic endpoint: tests Upstox 1min data fetch for a given instrument.
    Returns raw API response info so user can see exactly what's happening.
    """
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401

    body       = request.get_json() or {}
    instrument = body.get('instrument', 'NIFTY')
    instr      = INSTRUMENTS.get(instrument)
    if not instr:
        return jsonify({'error': 'Unknown instrument'}), 400

    try:
        be     = BacktestEngine(token)
        result = be.test_connection(instr['key'])
        result['instrument_label'] = instr['label']
        result['instrument_name']  = instrument
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/debug/sample-candles', methods=['POST'])
def api_sample_candles():
    """
    Returns the first 20 resampled 5-min candles for a 5-day window.
    Useful to verify data is flowing correctly before running a long backtest.
    """
    token = session.get('access_token')
    if not token:
        return jsonify({'error': 'Not logged in'}), 401

    body       = request.get_json() or {}
    instrument = body.get('instrument', 'NIFTY')
    instr      = INSTRUMENTS.get(instrument)
    if not instr:
        return jsonify({'error': 'Unknown instrument'}), 400

    from datetime import date, timedelta
    to_dt   = date.today()
    from_dt = to_dt - timedelta(days=5)

    try:
        be      = BacktestEngine(token)
        candles = be.fetch_candles(
            instr['key'],
            from_dt.strftime('%Y-%m-%d'),
            to_dt.strftime('%Y-%m-%d')
        )
        sample = [
            {**c, 'time': c['time'].strftime('%Y-%m-%d %H:%M')}
            for c in candles[:20]
        ]
        return jsonify({
            'instrument':    instr['label'],
            'total_candles': len(candles),
            'sample':        sample,
            'date_range':    f"{from_dt} -> {to_dt}"
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"""
╔══════════════════════════════════════════════════════╗
║   ORB + VWAP Auto-Trading System — Upstox API v2    ║
╠══════════════════════════════════════════════════════╣
║  URL    : http://localhost:{PORT}                       ║
║  Login  : http://localhost:{PORT}/login                 ║
╚══════════════════════════════════════════════════════╝
    """)
    app.run(port=PORT, debug=False, threaded=True)
