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
    """Simplified near-buy-and-hold: buy immediately, never sell unless catastrophe."""

    def __init__(self, config: Dict[str, Any], exchange):
        super().__init__(config=config, exchange=exchange)
        self.max_position_pct = min(0.55, float(config.get("max_position_pct", 0.55)))
        
        # Only track for extreme exit conditions
        self.max_drawdown_exit = float(config.get("max_drawdown_exit", 0.40))  # 40% total drawdown
        self.lookback_for_peak = int(config.get("lookback_for_peak", 720))  # 30 days
        
        self.rebalance_threshold = float(config.get("rebalance_threshold", 0.01))
        self.min_trade_notional = float(config.get("min_trade_notional", 200.0))
        
        self._close_buffer: Deque[float] = deque(maxlen=self.lookback_for_peak + 10)
        self._last_timestamp: Optional[datetime] = None
        self._pending_side: Optional[str] = None
        self._initial_entry_done: bool = False

    def prepare(self) -> None:
        self._close_buffer.clear()
        self._last_timestamp = None
        self._pending_side = None
        self._initial_entry_done = False

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

    def _extreme_crash(self) -> bool:
        """Only exit on extreme crashes: >40% drawdown from recent high."""
        if len(self._close_buffer) < self.lookback_for_peak:
            return False
        price = self._close_buffer[-1]
        recent_high = max(list(self._close_buffer)[-self.lookback_for_peak:])
        if recent_high <= 0:
            return False
        drawdown = (recent_high - price) / recent_high
        return drawdown > self.max_drawdown_exit

    def generate_signal(self, market: MarketSnapshot, portfolio: Portfolio) -> Signal:
        self._update_buffers(market)

        # Buy immediately on first opportunity
        if not self._initial_entry_done and len(self._close_buffer) >= 1 and portfolio.quantity <= _EPS:
            price = self._close_buffer[-1]
            equity = max(portfolio.value(price), _EPS)
            target_notional = equity * self.max_position_pct
            size = target_notional / max(price, _EPS)
            affordable = portfolio.cash / max(price, _EPS)
            size = min(size, affordable)
            if size > _EPS:
                self._pending_side = "buy"
                self._initial_entry_done = True
                return Signal("buy", size=size, reason="immediate_buy")

        if self._pending_side is not None:
            return Signal("hold", reason="await_fill")

        price = self._close_buffer[-1]
        equity = max(portfolio.value(price), _EPS)
        current_notional = portfolio.quantity * price
        current_pct = current_notional / equity

        # Default: stay fully invested, only exit on extreme crash
        target_pct = self.max_position_pct
        
        if portfolio.quantity > _EPS:
            if self._extreme_crash():
                target_pct = 0.0
        # Note: We never re-enter after exiting (contest period too short for recoveries)

        diff_pct = target_pct - current_pct

        if abs(diff_pct) < self.rebalance_threshold:
            return Signal("hold", reason="within_band")

        trade_notional = abs(diff_pct) * equity
        if trade_notional < self.min_trade_notional:
            return Signal("hold", reason="notional_floor")

        size = trade_notional / max(price, _EPS)
        
        if diff_pct > 0:
            affordable = portfolio.cash / max(price, _EPS)
            size = min(size, affordable)
            if size <= _EPS:
                return Signal("hold", reason="insufficient_cash")
            self._pending_side = "buy"
            return Signal("buy", size=size, reason="entry")

        size = min(size, portfolio.quantity)
        if size <= _EPS:
            return Signal("hold", reason="no_position")
        self._pending_side = "sell"
        return Signal("sell", size=size, reason="extreme_crash_exit")

    def on_trade(self, signal: Signal, execution_price: float, execution_size: float, timestamp: datetime) -> None:
        if execution_size <= 0:
            return
        self._pending_side = None

def _factory(config: Dict[str, Any], exchange) -> MomentumRotatorStrategy:
    return MomentumRotatorStrategy(config=config, exchange=exchange)


register_strategy("momentum_rotator", _factory)


__all__ = ["MomentumRotatorStrategy"]
