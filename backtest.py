"""
Backtesting Engine — Real Options P&L with BSM Fallback
=========================================================
How it works:
  1. Fetch underlying index 1-min candles from Upstox (chunked, resampled to 5-min)
  2. Run ORB+VWAP strategy on index to get entry/exit signals + timing
  3. On each signal, determine ATM strike and build NSE_FO option key
  4. Try to fetch real 1-min option candles from Upstox
  5. If Upstox has no data (common for expired contracts), fall back to
     Black-Scholes-Merton (BSM) pricing using the index price movement
     → entry premium  = BSM(index_at_entry, strike, T_to_expiry, IV)
     → exit  premium  = BSM(index_at_exit,  strike, T_to_expiry, IV)
     → P&L = (exit_premium - entry_premium) × lot_size × lots
  6. Trade log shows: option symbol, strike, expiry, entry ₹, exit ₹, P&L ₹
     and flags each trade ✅ Real (Upstox data) or 📊 BSM (calculated).

BSM default IVs (annualised):
  BANKNIFTY / bank stocks : 20 %
  NIFTY / IT stocks       : 16 %
  Other F&O stocks        : 22 %
"""

import math
import requests
import urllib.parse
import numpy as np
import calendar
from datetime import datetime, date, timedelta, timezone
from strategy import ORBVWAPStrategy

UPSTOX_BASE  = "https://api.upstox.com/v2"
CHUNK_DAYS   = 10
RESAMPLE_MIN = 5


# ──────────────────────────────────────────────────────────────────────
#  Safe-round helper
# ──────────────────────────────────────────────────────────────────────
def _r(value, decimals=2):
    try:
        v = float(value)
        return 0.0 if v != v else round(v, decimals)
    except (TypeError, ValueError):
        return 0.0


# ──────────────────────────────────────────────────────────────────────
#  India VIX helpers
# ──────────────────────────────────────────────────────────────────────
VIX_KEY = 'NSE_INDEX|India VIX'

def _vix_regime(vix) -> str:
    """Classify India VIX into trading-relevant regimes."""
    if vix is None:
        return 'Unknown'
    v = float(vix)
    if v < 13:   return 'Very Low (<13)'
    if v < 16:   return 'Low (13-16)'
    if v < 20:   return 'Normal (16-20)'
    if v < 25:   return 'High (20-25)'
    return 'Very High (>25)'

def _vix_color(vix) -> str:
    """CSS color for a VIX value."""
    if vix is None:     return 'var(--dim)'
    v = float(vix)
    if v < 13:          return 'var(--teal)'
    if v < 16:          return 'var(--green)'
    if v < 20:          return 'var(--amber)'
    return 'var(--red)'


# ──────────────────────────────────────────────────────────────────────
#  Black-Scholes-Merton helpers  (module-level, no class needed)
# ──────────────────────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bsm_price(spot: float, strike: float, T: float,
              r: float, sigma: float, opt_type: str) -> float:
    """
    Black-Scholes-Merton option price.
    spot     : current underlying price
    strike   : option strike price
    T        : time to expiry in years (must be > 0)
    r        : risk-free rate (annualised, e.g. 0.065)
    sigma    : implied volatility (annualised, e.g. 0.20)
    opt_type : 'CE' (call) or 'PE' (put)
    """
    if T <= 0:
        intrinsic = (max(0.0, spot - strike) if opt_type == 'CE'
                     else max(0.0, strike - spot))
        return max(round(intrinsic, 2), 0.05)

    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if opt_type == 'CE':
        price = (spot * _norm_cdf(d1)
                 - strike * math.exp(-r * T) * _norm_cdf(d2))
    else:
        price = (strike * math.exp(-r * T) * _norm_cdf(-d2)
                 - spot * _norm_cdf(-d1))

    return max(round(price, 2), 0.05)


def bsm_entry_exit(direction: str,
                   entry_spot: float, exit_spot: float,
                   strike: int,
                   expiry: date, trade_date: date,
                   entry_time_str: str, exit_time_str: str,
                   iv: float, r: float = 0.065):
    """
    Calculate BSM option premiums at entry and exit times.

    Returns (entry_premium, exit_premium) in ₹ per share.

    Time-to-expiry is adjusted for intraday hours elapsed so that
    theta decay is reflected within the same trading day.
    """
    opt_type = 'CE' if direction == 'LONG' else 'PE'

    cal_days = (expiry - trade_date).days
    cal_days = max(cal_days, 0)

    def hours_from_open(t_str: str) -> float:
        """Hours elapsed since 09:15 for a HH:MM string."""
        try:
            h, m = int(t_str[:2]), int(t_str[3:5])
            return max(0.0, (h * 60 + m - 9 * 60 - 15) / 60.0)
        except Exception:
            return 0.0

    elapsed_entry = hours_from_open(entry_time_str)
    elapsed_exit  = hours_from_open(exit_time_str)

    # Convert to years; subtract intraday hours elapsed
    T_entry = max(0.0001, (cal_days - elapsed_entry / 24.0) / 365.0)
    T_exit  = max(0.0001, (cal_days - elapsed_exit  / 24.0) / 365.0)

    entry_prem = bsm_price(entry_spot, float(strike), T_entry, r, iv, opt_type)
    exit_prem  = bsm_price(exit_spot,  float(strike), T_exit,  r, iv, opt_type)

    return entry_prem, exit_prem


# ======================================================================
class BacktestEngine:

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept':        'application/json'
        }
        self._opt_cache: dict = {}

    # ──────────────────────────────────────────────────────────────────
    #  Expiry calculators
    # ──────────────────────────────────────────────────────────────────
    def _get_expiry(self, trade_date: date, instrument_name: str) -> date:
        name = instrument_name.upper()
        if 'BANK' in name:
            return self._last_tuesday_gte(trade_date)
        elif 'NIFTY' in name:
            return self._next_tuesday_gte(trade_date)
        else:
            return self._last_thursday_gte(trade_date)

    @staticmethod
    def _next_tuesday_gte(ref: date) -> date:
        days = (1 - ref.weekday()) % 7
        return ref + timedelta(days=days)

    @staticmethod
    def _last_tuesday_gte(ref: date) -> date:
        y, m = ref.year, ref.month
        for _ in range(3):
            cal  = calendar.monthcalendar(y, m)
            tues = [w[1] for w in cal if w[1]]
            exp  = date(y, m, tues[-1])
            if exp >= ref:
                return exp
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return exp

    @staticmethod
    def _last_thursday_gte(ref: date) -> date:
        y, m = ref.year, ref.month
        for _ in range(3):
            cal   = calendar.monthcalendar(y, m)
            thurs = [w[3] for w in cal if w[3]]
            exp   = date(y, m, thurs[-1])
            if exp >= ref:
                return exp
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return exp

    # ──────────────────────────────────────────────────────────────────
    #  Option key builder
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_option_key(nse_symbol: str, expiry: date,
                          strike: int, opt_type: str) -> str:
        """NSE_FO|BANKNIFTY26MAY2654800CE  style."""
        code = expiry.strftime('%d%b%y').upper()   # e.g. 26MAY26
        return f"NSE_FO|{nse_symbol.upper()}{code}{int(strike)}{opt_type.upper()}"

    @staticmethod
    def _get_atm_strike(price: float, gap: int) -> int:
        return int(round(price / gap) * gap)

    # ──────────────────────────────────────────────────────────────────
    #  Index candle fetching
    # ──────────────────────────────────────────────────────────────────
    def fetch_candles(self, instrument_key: str,
                      from_date: str, to_date: str) -> list:
        """Fetch 5-min candles for date range (1-min chunks → resample)."""
        from_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
        to_dt   = datetime.strptime(to_date,   '%Y-%m-%d').date()
        all_1min, chunk = [], from_dt
        while chunk <= to_dt:
            end = min(chunk + timedelta(days=CHUNK_DAYS - 1), to_dt)
            all_1min.extend(
                self._fetch_1min_chunk(instrument_key,
                                       chunk.strftime('%Y-%m-%d'),
                                       end.strftime('%Y-%m-%d')))
            chunk = end + timedelta(days=1)
        return self._resample(all_1min, RESAMPLE_MIN) if all_1min else []

    def fetch_intraday(self, instrument_key: str) -> list:
        """Today's 5-min candles (live use)."""
        enc = urllib.parse.quote(instrument_key, safe='')
        url = f"{UPSTOX_BASE}/historical-candle/intraday/{enc}/1minute"
        try:
            r   = requests.get(url, headers=self.headers, timeout=15)
            raw = r.json().get('data', {}).get('candles', [])
            return self._resample(self._parse_raw(raw), RESAMPLE_MIN)
        except Exception:
            return []

    def test_connection(self, instrument_key: str) -> dict:
        to_dt   = date.today()
        from_dt = to_dt - timedelta(days=3)
        enc = urllib.parse.quote(instrument_key, safe='')
        url = (f"{UPSTOX_BASE}/historical-candle/{enc}/1minute"
               f"/{to_dt.strftime('%Y-%m-%d')}/{from_dt.strftime('%Y-%m-%d')}")
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            body = resp.json()
        except Exception as e:
            return {'error': str(e), 'url_tested': url}
        raw     = body.get('data', {}).get('candles', [])
        candles = self._parse_raw(raw)
        return {
            'http_status':      resp.status_code,
            'url_tested':       url,
            'raw_candle_count': len(raw),
            'parsed_count':     len(candles),
            'upstox_status':    body.get('status'),
            'upstox_errors':    body.get('errors', []),
            'first_candle':     ({**candles[0],  'time': candles[0]['time'].strftime('%Y-%m-%d %H:%M')}
                                  if candles else None),
            'last_candle':      ({**candles[-1], 'time': candles[-1]['time'].strftime('%Y-%m-%d %H:%M')}
                                  if candles else None),
        }

    # ──────────────────────────────────────────────────────────────────
    #  India VIX — daily data (Upstox → Yahoo Finance fallback)
    # ──────────────────────────────────────────────────────────────────
    def fetch_vix(self, from_date: str, to_date: str) -> dict:
        """
        Fetch India VIX daily closing values.
        Source priority:
          1. Upstox  (NSE_INDEX|India VIX  — may return empty for some accounts)
          2. Yahoo Finance  (^INDIAVIX — no auth needed, works on user's machine)
        Returns {YYYY-MM-DD: vix_close} or {} if both sources fail.
        """
        vix_map = self._fetch_vix_upstox(from_date, to_date)
        if vix_map:
            print(f"[VIX] ✅ Upstox: {len(vix_map)} daily values")
            return vix_map

        print("[VIX] Upstox returned no data — trying Yahoo Finance…")
        vix_map = self._fetch_vix_yahoo(from_date, to_date)
        if vix_map:
            print(f"[VIX] ✅ Yahoo Finance: {len(vix_map)} daily values")
        else:
            print("[VIX] ⚠ Both sources returned no data — VIX column will show —")
        return vix_map

    def _fetch_vix_upstox(self, from_date: str, to_date: str) -> dict:
        """Try to fetch VIX from Upstox historical candle API."""
        enc = urllib.parse.quote(VIX_KEY, safe='')
        url = (f"{UPSTOX_BASE}/historical-candle/{enc}/1day"
               f"/{to_date}/{from_date}")
        try:
            resp = requests.get(url, headers=self.headers, timeout=20)
            if resp.status_code != 200:
                print(f"[VIX-Upstox] HTTP {resp.status_code}")
                return {}
            raw = resp.json().get('data', {}).get('candles', [])
            if not raw:
                return {}
            IST = timezone(timedelta(hours=5, minutes=30))
            out = {}
            for c in raw:
                try:
                    ts  = datetime.fromisoformat(c[0].replace('Z', '+00:00'))
                    ist = ts.astimezone(IST).replace(tzinfo=None)
                    out[ist.strftime('%Y-%m-%d')] = round(float(c[4]), 2)
                except Exception:
                    continue
            return out
        except Exception as e:
            print(f"[VIX-Upstox] Exception: {e}")
            return {}

    def _fetch_vix_yahoo(self, from_date: str, to_date: str) -> dict:
        """
        Fetch India VIX from Yahoo Finance (ticker ^INDIAVIX).
        No API key required. Works on any internet-connected machine.
        """
        try:
            from datetime import timezone as _tz
            fd = datetime.strptime(from_date, '%Y-%m-%d')
            td = datetime.strptime(to_date,   '%Y-%m-%d')
            p1 = int(fd.replace(tzinfo=_tz.utc).timestamp())
            p2 = int(td.replace(tzinfo=_tz.utc).timestamp()) + 86400

            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"%5EINDIAVIX?interval=1d&period1={p1}&period2={p2}")
            hdrs = {'User-Agent': 'Mozilla/5.0 (compatible; trading-app/1.0)',
                    'Accept': 'application/json'}

            resp = requests.get(url, headers=hdrs, timeout=20)
            if resp.status_code != 200:
                print(f"[VIX-Yahoo] HTTP {resp.status_code}")
                return {}

            result = resp.json().get('chart', {}).get('result', [])
            if not result:
                return {}

            timestamps = result[0].get('timestamp', [])
            closes     = (result[0].get('indicators', {})
                                   .get('quote', [{}])[0]
                                   .get('close', []))
            IST = timezone(timedelta(hours=5, minutes=30))
            out = {}
            for ts, cl in zip(timestamps, closes):
                if cl is not None:
                    ist = datetime.fromtimestamp(
                        ts, tz=_tz.utc).astimezone(IST).replace(tzinfo=None)
                    out[ist.strftime('%Y-%m-%d')] = round(float(cl), 2)
            return out
        except Exception as e:
            print(f"[VIX-Yahoo] Exception: {e}")
            return {}


    def _fetch_opt_day(self, option_key: str, day: date) -> dict:
        """
        Fetch 1-min option candles for one day.
        Returns {HH:MM: candle}.  Cached.
        """
        cid = f"{option_key}|{day}"
        if cid in self._opt_cache:
            return self._opt_cache[cid]
        day_str = day.strftime('%Y-%m-%d')
        candles = self._fetch_1min_chunk(option_key, day_str, day_str)
        m = {c['time'].strftime('%H:%M'): c for c in candles}
        self._opt_cache[cid] = m
        tag = option_key.split('|')[1] if '|' in option_key else option_key
        print(f"[OPT] {tag} | {day_str} | {len(m)} candles")
        return m

    def _opt_price_at(self, opt_map: dict, time_str: str):
        """Option close at time_str (HH:MM), ±15 min fallback, else None."""
        if not opt_map:
            return None
        if time_str in opt_map:
            return float(opt_map[time_str]['close'])
        t0 = int(time_str[:2]) * 60 + int(time_str[3:5])
        best_k, best_d = None, 9999
        for k in opt_map:
            d = abs(int(k[:2]) * 60 + int(k[3:5]) - t0)
            if d < best_d:
                best_d, best_k = d, k
        return float(opt_map[best_k]['close']) if (best_k and best_d <= 15) else None

    # ──────────────────────────────────────────────────────────────────
    #  Main backtest run
    # ──────────────────────────────────────────────────────────────────
    def run(self, instrument_key: str, instrument_name: str,
            lot_size: int, strike_gap: int, nse_symbol: str,
            from_date: str, to_date: str,
            capital: float = 400_000, risk_pct: float = 1.0,
            or_minutes: int = 15, lots: int = 1,
            t1_ratio: float = 1.5, t2_ratio: float = 2.5,
            iv: float = 0.20) -> dict:
        """
        Full backtest with real options P&L.
        Falls back to BSM pricing if Upstox has no historical option data.
        """
        print(f"[BT] {instrument_name} | {from_date}→{to_date} | IV={iv*100:.0f}%")
        self._opt_cache.clear()

        idx = self.fetch_candles(instrument_key, from_date, to_date)
        if not idx:
            return {'error':
                    'No index data from Upstox.\n'
                    '1. Token expired — logout and login again.\n'
                    '2. Date range may exceed 1 year.\n'
                    '3. Use "Test Data Connection" in Settings.'}

        # Fetch India VIX daily data for the same range (single API call)
        vix_map = self.fetch_vix(from_date, to_date)

        print(f"[BT] {len(idx)} 5-min index candles fetched.")

        daily = {}
        for c in idx:
            daily.setdefault(c['time'].date(), []).append(c)
        daily = dict(sorted(daily.items()))

        trades, equity_curve = [], []
        running_pnl = max_peak = max_dd = 0.0
        real_count = 0
        min_c = max(4, or_minutes // 5 + 3)

        for day, day_candles in daily.items():
            mkt = [c for c in day_candles
                   if (c['time'].hour > 9 or
                       (c['time'].hour == 9 and c['time'].minute >= 15))
                   and (c['time'].hour < 15 or
                        (c['time'].hour == 15 and c['time'].minute <= 30))]
            if len(mkt) < min_c:
                continue

            expiry = self._get_expiry(day, instrument_name)

            strat = ORBVWAPStrategy(
                instrument=instrument_name, lot_size=lot_size,
                capital=capital, risk_pct=risk_pct,
                or_minutes=or_minutes, timeframe_min=5,
                t1_ratio=t1_ratio, t2_ratio=t2_ratio)

            day_pnl  = 0.0
            einfo    = None     # active trade record
            opt_map  = {}       # {HH:MM: candle} for today's option
            # Per-trade running totals
            opt_part = 0.0      # option P&L banked at T1
            idx_part = 0.0      # index P&L banked at T1
            t1_done  = False
            use_real = False    # True = Upstox data; False = BSM

            for i, candle in enumerate(mkt):
                is_last    = (i == len(mkt) - 1)
                force_time = (candle['time'].hour == 15
                              and candle['time'].minute >= 15)

                # ── Force-exit at 3:15 PM ──────────────────────────────
                if (force_time or is_last) and strat.position and einfo:
                    res      = strat.force_exit(candle['close'])
                    exit_tm  = candle['time'].strftime('%H:%M')
                    qty_rem  = lots * lot_size // 2 if t1_done else lots * lot_size
                    direction = einfo['direction']

                    # Index remaining P&L
                    idx_rem   = self._pt_pnl(direction, einfo['entry_price'],
                                             res['exit_price'], qty_rem)
                    idx_total = idx_part + idx_rem

                    # Option exit premium
                    opt_exit = self._opt_price_at(opt_map, exit_tm)
                    if opt_exit is None:
                        # BSM fallback
                        _, opt_exit = bsm_entry_exit(
                            direction, einfo['entry_price'], float(res['exit_price']),
                            einfo['strike'], expiry, day,
                            einfo['entry_time'], exit_tm, iv)

                    opt_rem   = (opt_exit - einfo['opt_entry_price']) * qty_rem
                    opt_total = opt_part + opt_rem

                    pnl       = opt_total
                    part_used = opt_part
                    day_pnl  += pnl - part_used

                    einfo.update({
                        'exit_time':      exit_tm,
                        'exit_price':     _r(res['exit_price']),
                        'opt_exit_price': _r(opt_exit),
                        'opt_pnl':        _r(opt_total),
                        'idx_pnl':        _r(idx_total),
                        'pnl':            _r(pnl),
                        'pnl_pts':        _r(pnl / max(lots * lot_size, 1)),
                        'exit_reason':    'Time exit 3:15 PM',
                        'data_source':    'Real' if use_real else 'BSM',
                        'opt_data_ok':    True,
                        'result':         'WIN' if pnl > 0 else 'LOSS'
                    })
                    trades.append(einfo)
                    if use_real:
                        real_count += 1
                    einfo = None
                    continue

                if force_time:
                    continue

                res    = strat.process_candle(candle)
                if not res:
                    continue
                action = res.get('action')

                # ── New entry ──────────────────────────────────────────
                if action in ('BUY', 'SELL'):
                    direction  = res['direction']
                    opt_type   = 'CE' if direction == 'LONG' else 'PE'
                    strike     = self._get_atm_strike(float(res['entry']), strike_gap)
                    opt_key    = self._build_option_key(nse_symbol, expiry, strike, opt_type)
                    opt_symbol = opt_key.split('|')[1] if '|' in opt_key else opt_key
                    entry_tm   = candle['time'].strftime('%H:%M')

                    # Try Upstox first
                    opt_map    = self._fetch_opt_day(opt_key, day)
                    opt_entry  = self._opt_price_at(opt_map, entry_tm)
                    use_real   = opt_entry is not None

                    if opt_entry is None:
                        # BSM fallback for entry
                        opt_entry, _ = bsm_entry_exit(
                            direction, float(res['entry']), float(res['entry']),
                            strike, expiry, day,
                            entry_tm, entry_tm, iv)

                    einfo = {
                        'date':          day.strftime('%Y-%m-%d'),
                        'instrument':    instrument_name,
                        'direction':     direction,
                        'entry_time':    entry_tm,
                        'entry_price':   res['entry'],
                        'stop_loss':     res['stop_loss'],
                        'target1':       res['target1'],
                        'target2':       res['target2'],
                        'risk_pts':      res['risk_pts'],
                        'lots':          lots,
                        'lot_size':      lot_size,
                        'or_high':       res['or_high'],
                        'or_low':        res['or_low'],
                        'vwap_entry':    res['vwap'],
                        # Option fields
                        'option_key':    opt_key,
                        'option_symbol': opt_symbol,
                        'option_type':   opt_type,
                        'strike':        strike,
                        'expiry':        expiry.strftime('%d-%b-%Y'),
                        'opt_entry_price': _r(opt_entry),
                        'opt_exit_price':  None,
                        'opt_pnl':         None,
                        'idx_pnl':         None,
                        'data_source':     'Real' if use_real else 'BSM',
                        'opt_data_ok':     True,
                        # India VIX on trade date
                        'india_vix':       vix_map.get(day.strftime('%Y-%m-%d')),
                        'vix_regime':      _vix_regime(vix_map.get(day.strftime('%Y-%m-%d'))),
                        'result':          'OPEN'
                    }
                    opt_part = 0.0
                    idx_part = 0.0
                    t1_done  = False

                # ── T1 partial exit ───────────────────────────────────
                elif action == 'EXIT_T1' and einfo:
                    exit_tm  = candle['time'].strftime('%H:%M')
                    half_qty = lots * lot_size // 2
                    direction = einfo['direction']

                    # Index partial
                    idx_part = self._pt_pnl(direction, einfo['entry_price'],
                                            res['exit_price'], half_qty)

                    # Option at T1
                    opt_t1 = self._opt_price_at(opt_map, exit_tm)
                    if opt_t1 is None:
                        _, opt_t1 = bsm_entry_exit(
                            direction, einfo['entry_price'], float(res['exit_price']),
                            einfo['strike'], expiry, day,
                            einfo['entry_time'], exit_tm, iv)

                    opt_part = (opt_t1 - einfo['opt_entry_price']) * half_qty
                    day_pnl += opt_part
                    t1_done  = True
                    einfo['t1_was_hit'] = True

                # ── SL or T2 full exit ────────────────────────────────
                elif action in ('EXIT_SL', 'EXIT_T2') and einfo:
                    exit_tm   = candle['time'].strftime('%H:%M')
                    qty_rem   = lots * lot_size // 2 if t1_done else lots * lot_size
                    direction = einfo['direction']

                    # Index
                    idx_rem   = self._pt_pnl(direction, einfo['entry_price'],
                                             res['exit_price'], qty_rem)
                    idx_total = idx_part + idx_rem

                    # Option exit
                    opt_exit = self._opt_price_at(opt_map, exit_tm)
                    if opt_exit is None:
                        _, opt_exit = bsm_entry_exit(
                            direction, einfo['entry_price'], float(res['exit_price']),
                            einfo['strike'], expiry, day,
                            einfo['entry_time'], exit_tm, iv)

                    opt_rem   = (opt_exit - einfo['opt_entry_price']) * qty_rem
                    opt_total = opt_part + opt_rem

                    pnl       = opt_total
                    day_pnl  += pnl - opt_part   # partial already in day_pnl

                    einfo.update({
                        'exit_time':      exit_tm,
                        'exit_price':     res['exit_price'],
                        'opt_exit_price': _r(opt_exit),
                        'opt_pnl':        _r(opt_total),
                        'idx_pnl':        _r(idx_total),
                        'pnl':            _r(pnl),
                        'pnl_pts':        _r(pnl / max(lots * lot_size, 1)),
                        'exit_reason':    res['reason'],
                        'data_source':    'Real' if use_real else 'BSM',
                        'opt_data_ok':    True,
                        'result':         'WIN' if pnl > 0 else 'LOSS'
                    })
                    trades.append(einfo)
                    if use_real:
                        real_count += 1
                    einfo   = None
                    t1_done = False

            # ── Equity curve ──────────────────────────────────────────
            running_pnl += day_pnl
            if running_pnl > max_peak:
                max_peak = running_pnl
            dd = max_peak - running_pnl
            if dd > max_dd:
                max_dd = dd

            if abs(day_pnl) > 0:
                equity_curve.append({
                    'date':           day.strftime('%Y-%m-%d'),
                    'day_pnl':        _r(day_pnl),
                    'cumulative_pnl': _r(running_pnl)
                })

        return self._compile(trades, equity_curve, running_pnl, max_dd,
                             len(daily), instrument_name,
                             from_date, to_date, capital, real_count)

    # ──────────────────────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _pt_pnl(direction: str, entry: float,
                exit_p: float, qty: int) -> float:
        if direction == 'LONG':
            return (float(exit_p) - float(entry)) * qty
        return (float(entry) - float(exit_p)) * qty

    def _compile(self, trades, equity_curve, total_pnl, max_dd,
                 total_days, name, fd, td, capital, real_count):
        done  = [t for t in trades if t.get('result') in ('WIN', 'LOSS')]
        wins  = [t for t in done   if t['result'] == 'WIN']
        losses= [t for t in done   if t['result'] == 'LOSS']
        n     = len(done)
        wr    = len(wins) / n * 100 if n else 0.0

        def smean(lst):
            v = [float(t['pnl']) for t in lst if t.get('pnl') is not None]
            return float(np.mean(v)) if v else 0.0

        aw  = smean(wins)
        al  = smean(losses)
        gw  = sum(float(t['pnl']) for t in wins   if t.get('pnl') is not None)
        gl  = abs(sum(float(t['pnl']) for t in losses if t.get('pnl') is not None))
        pf  = _r(gw / gl) if gl > 0 else 999.0
        bsm_count = n - real_count

        # ── India VIX breakdown ──────────────────────────────────────
        vix_buckets = [
            ('Very Low (<13)',  lambda v: v is not None and v < 13),
            ('Low (13–16)',     lambda v: v is not None and 13 <= v < 16),
            ('Normal (16–20)', lambda v: v is not None and 16 <= v < 20),
            ('High (20–25)',    lambda v: v is not None and 20 <= v < 25),
            ('Very High (>25)',lambda v: v is not None and v >= 25),
            ('No VIX data',    lambda v: v is None),
        ]
        vix_analysis = []
        for label, cond in vix_buckets:
            bt  = [t for t in done if cond(t.get('india_vix'))]
            if not bt:
                continue
            bw   = [t for t in bt if t.get('result') == 'WIN']
            bpnl = sum(float(t.get('pnl') or 0) for t in bt)
            vix_vals = [t['india_vix'] for t in bt if t.get('india_vix') is not None]
            vix_analysis.append({
                'regime':    label,
                'trades':    len(bt),
                'wins':      len(bw),
                'losses':    len(bt) - len(bw),
                'win_rate':  _r(len(bw) / len(bt) * 100),
                'total_pnl': _r(bpnl),
                'avg_vix':   _r(sum(vix_vals) / len(vix_vals)) if vix_vals else None,
            })

        return {
            'summary': {
                'instrument':    name,
                'from_date':     fd,
                'to_date':       td,
                'total_days':    total_days,
                'days_traded':   len(equity_curve),
                'total_trades':  n,
                'wins':          len(wins),
                'losses':        len(losses),
                'win_rate':      _r(wr),
                'total_pnl':     _r(total_pnl),
                'avg_win':       _r(aw),
                'avg_loss':      _r(al),
                'profit_factor': _r(pf),
                'max_drawdown':  _r(max_dd),
                'sharpe_ratio':  _r(self._sharpe([e['day_pnl'] for e in equity_curve])),
                'return_pct':    _r(total_pnl / capital * 100) if capital else 0.0,
                'expectancy':    _r(wr / 100 * aw + (1 - wr / 100) * al),
                'real_trades':   real_count,
                'bsm_trades':    bsm_count,
                'total_trades_n': n,
            },
            'vix_analysis': vix_analysis,
            'equity_curve': equity_curve,
            'trades': sorted(done, key=lambda x: x.get('date', ''), reverse=True)
        }

    @staticmethod
    def _sharpe(daily_pnls, rf=0.0):
        arr = np.array([float(v) for v in daily_pnls if v is not None], dtype=float)
        if len(arr) < 5:
            return 0.0
        std = np.std(arr)
        return 0.0 if std == 0 else float((np.mean(arr) - rf) / std * np.sqrt(250))

    # ──────────────────────────────────────────────────────────────────
    #  Raw data parsing / resampling
    # ──────────────────────────────────────────────────────────────────
    def _fetch_1min_chunk(self, instrument_key: str,
                          from_date: str, to_date: str) -> list:
        enc = urllib.parse.quote(instrument_key, safe='')
        url = (f"{UPSTOX_BASE}/historical-candle/"
               f"{enc}/1minute/{to_date}/{from_date}")
        try:
            resp = requests.get(url, headers=self.headers, timeout=30)
            if resp.status_code == 401:
                print("[BT] 401 — token expired.")
                return []
            if resp.status_code != 200:
                print(f"[BT] HTTP {resp.status_code} for {from_date}→{to_date}")
                return []
            raw = resp.json().get('data', {}).get('candles', [])
            if raw:
                print(f"[BT] {from_date}→{to_date}: {len(raw)} 1-min candles")
            return self._parse_raw(raw)
        except Exception as e:
            print(f"[BT] Exception: {e}")
            return []

    @staticmethod
    def _parse_raw(raw: list) -> list:
        IST = timezone(timedelta(hours=5, minutes=30))
        out = []
        for c in raw:
            try:
                ts  = datetime.fromisoformat(c[0].replace('Z', '+00:00'))
                ist = ts.astimezone(IST).replace(tzinfo=None)
                out.append({'time': ist,
                            'open':   float(c[1]), 'high':   float(c[2]),
                            'low':    float(c[3]), 'close':  float(c[4]),
                            'volume': int(c[5]),
                            'oi':     int(c[6]) if len(c) > 6 else 0})
            except Exception:
                continue
        out.sort(key=lambda x: x['time'])
        return out

    @staticmethod
    def _resample(candles: list, period: int) -> list:
        if not candles:
            return []
        result = []
        bt = bo = bh = bl = bc = bv = boi = None
        for c in candles:
            t    = c['time']
            mins = (t.hour * 60 + t.minute) // period * period
            sh, sm = divmod(mins, 60)
            st = t.replace(hour=sh, minute=sm, second=0, microsecond=0)
            if bt is None or st != bt:
                if bt is not None:
                    result.append({'time': bt, 'open': bo, 'high': bh,
                                   'low': bl, 'close': bc, 'volume': bv, 'oi': boi})
                bt, bo, bh, bl, bc, bv, boi = (
                    st, c['open'], c['high'], c['low'],
                    c['close'], c['volume'], c.get('oi', 0))
            else:
                bh  = max(bh, c['high'])
                bl  = min(bl, c['low'])
                bc  = c['close']
                bv += c['volume']
                boi = c.get('oi', 0)
        if bt is not None:
            result.append({'time': bt, 'open': bo, 'high': bh,
                           'low': bl, 'close': bc, 'volume': bv, 'oi': boi})
        return result
