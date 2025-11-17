#!/usr/bin/env python3
"""Reproducible backtest runner for the Momentum Rotator strategy."""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
import yfinance as yf

import os
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = ROOT / "base-bot-template"
STRATEGY_PATH = ROOT / "buyhold-maximizer-strategy"

sys.path.insert(0, str(BASE_PATH))
sys.path.insert(0, str(STRATEGY_PATH))

from exchange_interface import MarketSnapshot  # noqa: E402
from strategy_interface import Portfolio, Signal  # noqa: E402
from buyhold_maximizer import MomentumRotatorStrategy  # noqa: E402

FEE_RATE = 0.001  # 10 bps per side
EXECUTION_LAG = 1  # hours
STARTING_CASH = 10_000.0
REQUIRED_HISTORY = 200


@dataclass
class Trade:
    symbol: str
    side: str
    size: float
    price: float
    timestamp: datetime
    pnl: float


@dataclass
class BacktestResult:
    symbol: str
    final_equity: float
    total_return: float
    max_drawdown: float
    trades: List[Trade]
    equity_curve: pd.Series

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for trade in self.trades if trade.pnl > 0)
        return wins / len(self.trades)


class OfflineExchange:
    name = "offline"


class MomentumBacktester:
    def __init__(self, symbol: str, frame: pd.DataFrame, strategy_params: Optional[Dict] = None):
        self.symbol = symbol
        self.frame = frame
        self.strategy = MomentumRotatorStrategy(strategy_params or {}, OfflineExchange())
        self.strategy.prepare()
        self.portfolio = Portfolio(symbol=symbol, cash=STARTING_CASH)
        self.pending_order: Optional[Dict] = None
        self.lots: List[Dict[str, float]] = []
        self.trades: List[Trade] = []
        self.equity: List[float] = []
        self.timestamps: List[datetime] = []

    def run(self) -> BacktestResult:
        closes = self.frame["Close"].tolist()
        for idx, (ts, row) in enumerate(self.frame.iterrows()):
            price = float(row["Close"])
            timestamp = ts.to_pydatetime()

            self._maybe_execute_pending(idx, price, timestamp)

            start_idx = max(0, idx - REQUIRED_HISTORY + 1)
            history = closes[start_idx : idx + 1]
            snapshot = MarketSnapshot(
                symbol=self.symbol,
                prices=history,
                current_price=price,
                timestamp=timestamp,
            )

            signal = self.strategy.generate_signal(snapshot, self.portfolio)
            self._handle_signal(idx, signal, price, timestamp)

            equity_value = self.portfolio.value(price)
            self.equity.append(equity_value)
            self.timestamps.append(timestamp)

        result = BacktestResult(
            symbol=self.symbol,
            final_equity=self.equity[-1] if self.equity else STARTING_CASH,
            total_return=(self.equity[-1] / STARTING_CASH - 1.0) if self.equity else 0.0,
            max_drawdown=self._max_drawdown(),
            trades=self.trades,
            equity_curve=pd.Series(self.equity, index=pd.DatetimeIndex(self.timestamps)),
        )
        return result

    def _handle_signal(self, idx: int, signal: Signal, price: float, ts: datetime) -> None:
        if signal.action not in {"buy", "sell"} or signal.size <= 0:
            return
        if self.pending_order is not None:
            return
        execute_index = idx + EXECUTION_LAG
        self.pending_order = {
            "action": signal.action,
            "size": signal.size,
            "execute_index": execute_index,
            "signal": signal,
        }

    def _maybe_execute_pending(self, idx: int, price: float, ts: datetime) -> None:
        if not self.pending_order:
            return
        if idx < self.pending_order["execute_index"]:
            return
        order = self.pending_order
        self.pending_order = None
        if order["action"] == "buy":
            self._execute_buy(order, price, ts)
        else:
            self._execute_sell(order, price, ts)

    def _execute_buy(self, order: Dict, price: float, ts: datetime) -> None:
        size = float(order["size"])
        if size <= 0 or price <= 0:
            return
        max_affordable = self.portfolio.cash / (price * (1 + FEE_RATE))
        equity_before = self.portfolio.value(price)
        max_notional = equity_before * 0.55
        size_cap = max_notional / price if price > 0 else 0.0
        size = min(size, max_affordable, size_cap)
        if size <= 0:
            return

        cost = size * price
        fee = cost * FEE_RATE
        total = cost + fee
        self.portfolio.cash -= total
        self.portfolio.quantity += size
        self.lots.append({"size": size, "price": price})
        self.strategy.on_trade(order["signal"], price, size, ts)

    def _execute_sell(self, order: Dict, price: float, ts: datetime) -> None:
        size = min(float(order["size"]), self.portfolio.quantity)
        if size <= 0 or price <= 0:
            return

        proceeds = size * price
        fee = proceeds * FEE_RATE
        self.portfolio.cash += proceeds - fee
        self.portfolio.quantity -= size
        pnl = self._realize_pnl(size, price)
        self.trades.append(Trade(self.symbol, "sell", size, price, ts, pnl))
        self.strategy.on_trade(order["signal"], price, size, ts)

    def _realize_pnl(self, size: float, exit_price: float) -> float:
        remaining = size
        pnl = 0.0
        new_lots = []
        for lot in self.lots:
            if remaining <= 0:
                new_lots.append(lot)
                continue
            lot_size = lot["size"]
            lot_price = lot["price"]
            if lot_size <= remaining + 1e-12:
                pnl += (exit_price - lot_price) * lot_size
                remaining -= lot_size
            else:
                pnl += (exit_price - lot_price) * remaining
                new_lots.append({"size": lot_size - remaining, "price": lot_price})
                remaining = 0
        self.lots = new_lots
        return pnl

    def _max_drawdown(self) -> float:
        if not self.equity:
            return 0.0
        peak = -math.inf
        max_dd = 0.0
        for value in self.equity:
            peak = max(peak, value)
            dd = (peak - value) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd


def _load_data(symbol: str) -> pd.DataFrame:
    data = yf.download(
        tickers=symbol,
        start="2024-01-01",
        end="2024-07-01",
        interval="1h",
        progress=False,
        group_by="ticker",
        auto_adjust=False,
    )
    if data.empty:
        raise RuntimeError(f"No data returned for {symbol}")
    if isinstance(data.columns, pd.MultiIndex):
        data = data.droplevel(0, axis=1)
    data = data[["Open", "High", "Low", "Close"]].dropna()
    data.index = data.index.tz_localize(None)
    return data


def run_strategy(symbols: Iterable[str], strategy_params: Optional[Dict] = None) -> Dict[str, BacktestResult]:
    outcomes: Dict[str, BacktestResult] = {}
    for symbol in symbols:
        frame = _load_data(symbol)
        runner = MomentumBacktester(symbol, frame, strategy_params)
        outcomes[symbol] = runner.run()
    return outcomes


def summarise(results: Dict[str, BacktestResult]) -> None:
    combined_start = STARTING_CASH * len(results)
    combined_end = sum(res.final_equity for res in results.values())
    combined_return = combined_end / combined_start - 1.0

    print("\n=== Momentum Rotator Backtest Summary ===")
    for symbol, result in results.items():
        print(
            f"{symbol}: Return={result.total_return:.2%}, "
            f"MaxDD={result.max_drawdown:.2%}, Trades={result.trade_count}, "
            f"WinRate={result.win_rate:.2%}"
        )
    print(f"Combined Return: {combined_return:.2%}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Jan-Jun 2024 backtest")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC-USD", "ETH-USD"],
        help="Symbols to run (default: BTC-USD ETH-USD)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    # Optimized live trading parameters
    params = {
        "short_ma": 96,              # 4-day moving average (reduced whipsaws)
        "long_ma": 336,              # 14-day moving average (stronger trend)
        "confirmation_bars": 3,      # Require 3 bars of uptrend before entry
        "max_drawdown_exit": 0.15,   # Exit on 15% drawdown
        "lookback_for_peak": 336,    # 14-day peak tracking
        "rebalance_threshold": 0.01,
        "max_position_pct": 0.55,    # Contest maximum
        "min_trade_notional": 200.0,
    }
    results = run_strategy(args.symbols, params)
    summarise(results)
