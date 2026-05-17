"""
ORB + VWAP Combined Strategy Engine
====================================
Opening Range Breakout (ORB) + VWAP Confirmation
For Nifty, BankNifty and top NSE F&O stocks.

BUG FIX (NoneType __round__ error):
  _close_position() sets self._stop_loss / self._target2 to None.
  The old code tried to round() those AFTER closing — crash.
  Fix: capture exit_price into a local variable BEFORE _close_position().

Entry Rules:
  LONG  : 5-min candle CLOSES above OR High AND close > VWAP AND vol OK
  SHORT : 5-min candle CLOSES below OR Low  AND close < VWAP AND vol OK

Exit Rules:
  Stop Loss  : OR Low  (LONG)  / OR High (SHORT)
  Target 1   : Entry ± 1.5×risk → exit 50%, trail SL to entry
  Target 2   : Entry ± 2.5×risk → exit remaining 50%
  Time Exit  : Force exit at 3:15 PM
"""

import numpy as np
from datetime import datetime


def _r(value, decimals=2):
    """Safe round — returns 0.0 if value is None or not numeric."""
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return 0.0


class ORBVWAPStrategy:

    def __init__(self, instrument: str, lot_size: int, capital: float,
                 risk_pct: float = 1.0, or_minutes: int = 15,
                 timeframe_min: int = 5, t1_ratio: float = 1.5,
                 t2_ratio: float = 2.5, max_lots: int = 10):

        self.instrument    = instrument
        self.lot_size      = lot_size
        self.capital       = capital
        self.risk_pct      = risk_pct
        self.or_minutes    = or_minutes
        self.timeframe_min = timeframe_min
        self.t1_ratio      = t1_ratio
        self.t2_ratio      = t2_ratio
        self.max_lots      = max_lots
        self.or_candles    = max(1, or_minutes // timeframe_min)

        self._reset_session()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def process_candle(self, candle: dict):
        """
        Feed one completed candle. Returns a signal dict or None.
        candle = {time: datetime, open, high, low, close, volume}
        """
        self._session_candles.append(candle)
        self._candle_count += 1
        self._vwap = self._calc_vwap(self._session_candles)

        # ── Phase 1: build the Opening Range ──
        if self._candle_count <= self.or_candles:
            if self._or_high is None:
                self._or_high = candle['high']
                self._or_low  = candle['low']
            else:
                self._or_high = max(self._or_high, candle['high'])
                self._or_low  = min(self._or_low,  candle['low'])

            if self._candle_count == self.or_candles:
                self._or_complete = True

            return {
                'action':   'BUILDING_OR',
                'or_high':  _r(self._or_high),
                'or_low':   _r(self._or_low),
                'vwap':     _r(self._vwap),
                'candle':   self._candle_count,
                'of':       self.or_candles
            }

        if not self._or_complete:
            return None

        # ── Phase 2: manage open position ──
        if self._position:
            return self._check_exits(candle)

        # ── Phase 3: look for entry signal (one per session) ──
        if self._trade_taken:
            return {
                'action':  'WAIT',
                'vwap':    _r(self._vwap),
                'or_high': _r(self._or_high),
                'or_low':  _r(self._or_low)
            }

        # No new entries after 2:00 PM
        ct = candle.get('time')
        if isinstance(ct, datetime) and ct.hour >= 14:
            return {'action': 'TOO_LATE', 'vwap': _r(self._vwap)}

        # Volume filter: at least 75% of session average
        avg_vol = (np.mean([c['volume'] for c in self._session_candles])
                   if self._session_candles else 1)
        vol_ok  = candle['volume'] >= avg_vol * 0.75

        # LONG signal
        if (candle['close'] > self._or_high and
                candle['close'] > self._vwap and vol_ok):
            return self._enter('LONG', candle)

        # SHORT signal
        if (candle['close'] < self._or_low and
                candle['close'] < self._vwap and vol_ok):
            return self._enter('SHORT', candle)

        return {
            'action':  'WAIT',
            'vwap':    _r(self._vwap),
            'or_high': _r(self._or_high),
            'or_low':  _r(self._or_low)
        }

    def force_exit(self, price: float):
        """Force-close position at given price (end-of-session call)."""
        if not self._position:
            return None
        # Capture everything before closing
        exit_price = float(price)
        pnl = self._calc_pnl(exit_price, self._qty)
        self._close_position()
        return {
            'action':     'EXIT_TIME',
            'exit_price': _r(exit_price),
            'pnl':        _r(pnl),
            'reason':     'Time-based exit at 3:15 PM'
        }

    def reset_session(self):
        self._reset_session()

    # ── Read-only properties ──
    @property
    def position(self):      return self._position
    @property
    def entry_price(self):   return self._entry_price
    @property
    def stop_loss(self):     return self._stop_loss
    @property
    def target1(self):       return self._target1
    @property
    def target2(self):       return self._target2
    @property
    def vwap(self):          return _r(self._vwap) if self._vwap else 0
    @property
    def or_high(self):       return _r(self._or_high) if self._or_high else 0
    @property
    def or_low(self):        return _r(self._or_low)  if self._or_low  else 0
    @property
    def lots(self):          return self._lots
    @property
    def qty(self):           return self._qty
    @property
    def t1_hit(self):        return self._t1_hit

    def unrealized_pnl(self, current_price: float) -> float:
        if not self._position:
            return 0.0
        return _r(self._calc_pnl(current_price, self._qty))

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _reset_session(self):
        self._position       = None
        self._entry_price    = None
        self._stop_loss      = None
        self._target1        = None
        self._target2        = None
        self._t1_hit         = False
        self._lots           = 0
        self._qty            = 0
        self._or_high        = None
        self._or_low         = None
        self._or_complete    = False
        self._vwap           = 0.0
        self._candle_count   = 0
        self._trade_taken    = False
        self._session_candles = []

    def _calc_vwap(self, candles: list) -> float:
        """
        Calculate session VWAP.
        Fallback: when all volumes are 0 (common for NSE_INDEX instruments
        like Nifty/BankNifty returned by Upstox), use a simple unweighted
        average of typical prices. Without this, VWAP = 0 which makes
        SHORT signals (close < VWAP) impossible since prices are never < 0.
        """
        if not candles:
            return 0.0
        cum_vol    = sum(c['volume'] for c in candles)
        if cum_vol > 0:
            # Standard volume-weighted average price
            cum_tp_vol = sum(
                (c['high'] + c['low'] + c['close']) / 3.0 * c['volume']
                for c in candles
            )
            return cum_tp_vol / cum_vol
        else:
            # Fallback for zero-volume index candles (NSE_INDEX|Nifty Bank etc.)
            # Use unweighted mean of typical prices so VWAP is a real price level
            return sum(
                (c['high'] + c['low'] + c['close']) / 3.0
                for c in candles
            ) / len(candles)

    def _size_lots(self, entry: float, stop: float) -> int:
        risk_per_lot = abs(entry - stop) * self.lot_size
        if risk_per_lot <= 0:
            return 1
        max_risk = self.capital * self.risk_pct / 100.0
        lots = max(1, int(max_risk / risk_per_lot))
        return min(lots, self.max_lots)

    def _enter(self, direction: str, candle: dict) -> dict:
        entry = float(candle['close'])

        if direction == 'LONG':
            sl   = float(self._or_low)
            risk = entry - sl
            t1   = entry + self.t1_ratio * risk
            t2   = entry + self.t2_ratio * risk
        else:
            sl   = float(self._or_high)
            risk = sl - entry
            t1   = entry - self.t1_ratio * risk
            t2   = entry - self.t2_ratio * risk

        if risk <= 0:
            return {'action': 'INVALID_RISK', 'vwap': _r(self._vwap)}

        lots = self._size_lots(entry, sl)

        self._position    = direction
        self._entry_price = entry
        self._stop_loss   = sl
        self._target1     = t1
        self._target2     = t2
        self._t1_hit      = False
        self._lots        = lots
        self._qty         = lots * self.lot_size
        self._trade_taken = True

        action = 'BUY' if direction == 'LONG' else 'SELL'
        vwap_val = _r(self._vwap)
        side_word = 'above' if direction == 'LONG' else 'below'
        level_word = 'high' if direction == 'LONG' else 'low'

        return {
            'action':    action,
            'direction': direction,
            'entry':     _r(entry),
            'stop_loss': _r(sl),
            'target1':   _r(t1),
            'target2':   _r(t2),
            'risk_pts':  _r(risk),
            'lots':      lots,
            'qty':       self._qty,
            'vwap':      vwap_val,
            'or_high':   _r(self._or_high),
            'or_low':    _r(self._or_low),
            'reason': (
                f"ORB {'breakout' if direction=='LONG' else 'breakdown'} "
                f"({side_word} OR {level_word}) + "
                f"{side_word} VWAP ({vwap_val}) + volume confirmed"
            )
        }

    def _check_exits(self, candle: dict) -> dict:
        """
        Check stop loss, T1, and T2 for the open position.

        CRITICAL: Always capture exit_price into a LOCAL variable
        BEFORE calling _close_position(), which nullifies self._stop_loss,
        self._target1, self._target2, etc. Failing to do so causes:
          TypeError: type NoneType doesn't define __round__ method
        """
        if self._position == 'LONG':

            # ── Stop Loss ──
            if candle['low'] <= self._stop_loss:
                exit_price = float(self._stop_loss)   # ← capture BEFORE close
                pnl        = self._calc_pnl(exit_price, self._qty)
                self._close_position()                 # ← now safe to close
                return {
                    'action':     'EXIT_SL',
                    'exit_price': _r(exit_price),
                    'pnl':        _r(pnl),
                    'reason':     'Stop loss triggered'
                }

            # ── Target 1 (partial 50% exit) ──
            if not self._t1_hit and candle['high'] >= self._target1:
                exit_price      = float(self._target1)  # ← capture BEFORE modifying
                half_qty        = self._qty // 2
                pnl             = self._calc_pnl(exit_price, half_qty)
                self._t1_hit    = True
                self._qty      -= half_qty
                self._stop_loss = self._entry_price      # trail SL to breakeven
                return {
                    'action':     'EXIT_T1',
                    'exit_price': _r(exit_price),
                    'pnl':        _r(pnl),
                    'qty_exited': half_qty,
                    'reason':     'Target 1 hit — 50% exited, SL moved to entry'
                }

            # ── Target 2 (full exit of remaining 50%) ──
            if self._t1_hit and candle['high'] >= self._target2:
                exit_price = float(self._target2)       # ← capture BEFORE close
                pnl        = self._calc_pnl(exit_price, self._qty)
                self._close_position()                  # ← now safe to close
                return {
                    'action':     'EXIT_T2',
                    'exit_price': _r(exit_price),
                    'pnl':        _r(pnl),
                    'reason':     'Target 2 hit — full exit'
                }

        elif self._position == 'SHORT':

            # ── Stop Loss ──
            if candle['high'] >= self._stop_loss:
                exit_price = float(self._stop_loss)     # ← capture BEFORE close
                pnl        = self._calc_pnl(exit_price, self._qty)
                self._close_position()                  # ← now safe to close
                return {
                    'action':     'EXIT_SL',
                    'exit_price': _r(exit_price),
                    'pnl':        _r(pnl),
                    'reason':     'Stop loss triggered'
                }

            # ── Target 1 (partial 50% exit) ──
            if not self._t1_hit and candle['low'] <= self._target1:
                exit_price      = float(self._target1)  # ← capture BEFORE modifying
                half_qty        = self._qty // 2
                pnl             = self._calc_pnl(exit_price, half_qty)
                self._t1_hit    = True
                self._qty      -= half_qty
                self._stop_loss = self._entry_price      # trail SL to breakeven
                return {
                    'action':     'EXIT_T1',
                    'exit_price': _r(exit_price),
                    'pnl':        _r(pnl),
                    'qty_exited': half_qty,
                    'reason':     'Target 1 hit — 50% exited, SL moved to entry'
                }

            # ── Target 2 (full exit of remaining 50%) ──
            if self._t1_hit and candle['low'] <= self._target2:
                exit_price = float(self._target2)       # ← capture BEFORE close
                pnl        = self._calc_pnl(exit_price, self._qty)
                self._close_position()                  # ← now safe to close
                return {
                    'action':     'EXIT_T2',
                    'exit_price': _r(exit_price),
                    'pnl':        _r(pnl),
                    'reason':     'Target 2 hit — full exit'
                }

        # ── Hold ──
        return {
            'action':     'HOLD',
            'position':   self._position,
            'unrealized': _r(self.unrealized_pnl(candle['close']))
        }

    def _calc_pnl(self, exit_price: float, qty: int) -> float:
        """Compute P&L. Must be called BEFORE _close_position()."""
        if self._position == 'LONG':
            return (float(exit_price) - float(self._entry_price)) * qty
        elif self._position == 'SHORT':
            return (float(self._entry_price) - float(exit_price)) * qty
        return 0.0

    def _close_position(self):
        """Reset all position fields to None / zero."""
        self._position    = None
        self._entry_price = None
        self._stop_loss   = None
        self._target1     = None
        self._target2     = None
        self._t1_hit      = False
        self._qty         = 0
