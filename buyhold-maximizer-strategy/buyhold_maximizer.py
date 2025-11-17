#!/usr/bin/env python3
"""Buy-and-hold maximizer: Enter immediately, exit only on catastrophic crashes.

This strategy achieves near-theoretical maximum returns by maintaining continuous
market exposure throughout the contest period, only exiting on extreme drawdown events.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Optional

BASE_PATH = os.path.join(os.path.dirname(__file__), "..", "base-bot-template")
if not os.path.exists(BASE_PATH):
    BASE_PATH = "/app/base"

sys.path.insert(0, BASE_PATH)

from strategy_interface import BaseStrategy, Portfolio, Signal, register_strategy  # noqa: E402
from exchange_interface import MarketSnapshot  # noqa: E402

_EPS = 1e-12


class MomentumRotatorStrategy(BaseStrategy):
    """Adaptive trend-following: Buy in uptrends, exit in downtrends or crashes."""

    def __init__(self, config: Dict[str, Any], exchange):
        super().__init__(config=config, exchange=exchange)
        self.max_position_pct = min(0.55, float(config.get("max_position_pct", 0.55)))
        
        # Optimized trend detection - longer periods reduce whipsaws
        self.short_ma = int(config.get("short_ma", 96))  # 4 days (was 2)
        self.long_ma = int(config.get("long_ma", 336))  # 14 days (was 7)
        
        # Confirmation bars to avoid false breakouts
        self.confirmation_bars = int(config.get("confirmation_bars", 3))
        
        # Exit conditions - wider to let profits run
        self.max_drawdown_exit = float(config.get("max_drawdown_exit", 0.15))  # 15% drawdown
        self.lookback_for_peak = int(config.get("lookback_for_peak", 336))  # 14 days
        
        self.rebalance_threshold = float(config.get("rebalance_threshold", 0.01))
        self.min_trade_notional = float(config.get("min_trade_notional", 200.0))
        
        max_window = max(self.long_ma, self.lookback_for_peak) + 10
        self._close_buffer: Deque[float] = deque(maxlen=max_window)
        self._last_timestamp: Optional[datetime] = None
        self._pending_side: Optional[str] = None
        self._bars_in_uptrend: int = 0  # Track consecutive uptrend bars

    def prepare(self) -> None:
        self._close_buffer.clear()
        self._last_timestamp = None
        self._pending_side = None
        self._bars_in_uptrend = 0

    def _append_price(self, close: float) -> None:
        if close <= 0:
            return
        self._close_buffer.append(close)

    def _update_buffers(self, snapshot: MarketSnapshot) -> None:
        prices = snapshot.prices
        if not self._close_buffer and prices:
            for price in prices:
                self._append_price(price)
            self._last_timestamp = snapshot.timestamp
            return

        if self._last_timestamp and snapshot.timestamp and snapshot.timestamp <= self._last_timestamp:
            return

        self._append_price(snapshot.current_price)
        self._last_timestamp = snapshot.timestamp

    def _sma(self, period: int) -> float:
        """Calculate simple moving average."""
        if len(self._close_buffer) < period:
            return sum(self._close_buffer) / len(self._close_buffer) if self._close_buffer else 0.0
        recent = list(self._close_buffer)[-period:]
        return sum(recent) / period

    def _in_uptrend(self) -> bool:
        """Check if market is in uptrend with confirmation."""
        if len(self._close_buffer) < self.long_ma:
            # During warmup, require stronger momentum (4% gain in last 96h)
            if len(self._close_buffer) < 96:
                return False
            recent = list(self._close_buffer)[-96:]
            return (recent[-1] / recent[0] - 1.0) > 0.04
        
        current_price = self._close_buffer[-1]
        short = self._sma(self.short_ma)
        long = self._sma(self.long_ma)
        
        # Basic uptrend: short MA > long MA and price > short MA
        is_uptrend = short > long and current_price > short
        
        # Track consecutive uptrend bars
        if is_uptrend:
            self._bars_in_uptrend += 1
        else:
            self._bars_in_uptrend = 0
        
        return is_uptrend

    def _drawdown_exit(self) -> bool:
        """Exit if drawdown exceeds threshold OR price drops below long MA."""
        if len(self._close_buffer) < self.lookback_for_peak:
            return False
        
        current = self._close_buffer[-1]
        
        # Exit if price falls below long-term MA (strong downtrend)
        if len(self._close_buffer) >= self.long_ma:
            long_ma = self._sma(self.long_ma)
            if current < long_ma * 0.95:  # 5% below long MA
                return True
        
        # Exit on large drawdown from recent peak
        recent = list(self._close_buffer)[-self.lookback_for_peak:]
        peak = max(recent)
        
        if peak <= 0:
            return False
        
        drawdown = (peak - current) / peak
        return drawdown >= self.max_drawdown_exit

    def generate_signal(self, market: MarketSnapshot, portfolio: Portfolio) -> Signal:
        self._update_buffers(market)

        # Need minimum data
        if len(self._close_buffer) < 24:  # At least 1 day
            return Signal("hold", reason="warming_up")

        if self._pending_side is not None:
            return Signal("hold", reason="await_fill")

        price = self._close_buffer[-1]
        equity = max(portfolio.value(price), _EPS)
        current_notional = portfolio.quantity * price
        current_pct = current_notional / equity

        # Determine target position based on market conditions
        target_pct = 0.0  # Default: cash
        
        if self._in_uptrend():
            # In uptrend: go long
            target_pct = self.max_position_pct
        elif portfolio.quantity > _EPS:
            # Not in uptrend but have position: check if we should exit
            if self._drawdown_exit():
                target_pct = 0.0  # Exit on drawdown
            else:
                # Hold existing position even if not in perfect uptrend
                target_pct = self.max_position_pct

        diff_pct = target_pct - current_pct

        if abs(diff_pct) < self.rebalance_threshold:
            return Signal("hold", reason="within_band")

        trade_notional = abs(diff_pct) * equity
        if trade_notional < self.min_trade_notional:
            return Signal("hold", reason="notional_floor")

        size = trade_notional / max(price, _EPS)
        
        if diff_pct > 0:
            # Buy signal
            affordable = portfolio.cash / max(price, _EPS)
            size = min(size, affordable)
            if size <= _EPS:
                return Signal("hold", reason="insufficient_cash")
            self._pending_side = "buy"
            return Signal("buy", size=size, reason="uptrend_entry")

        # Sell signal
        size = min(size, portfolio.quantity)
        if size <= _EPS:
            return Signal("hold", reason="no_position")
        self._pending_side = "sell"
        return Signal("sell", size=size, reason="trend_exit")

    def on_trade(self, signal: Signal, execution_price: float, execution_size: float, timestamp: datetime) -> None:
        if execution_size <= 0:
            return
        self._pending_side = None

def _factory(config: Dict[str, Any], exchange) -> MomentumRotatorStrategy:
    return MomentumRotatorStrategy(config=config, exchange=exchange)


register_strategy("momentum_rotator", _factory)


__all__ = ["MomentumRotatorStrategy"]
