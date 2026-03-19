"""
backtest/run_backtest.py — CLI-запуск бэктеста.

Запуск::

    python backtest/run_backtest.py --data data/snapshots.csv --symbol BTCUSDT
    python backtest/run_backtest.py --synthetic --n 50000 --sweep
"""
from __future__ import annotations

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import yaml
from loguru import logger

from backtest.engine import (
    BacktestEngine,
    load_snapshots_from_csv,
    generate_synthetic_snapshots,
)


def parse_args():
    """Разбирает аргументы командной строки."""
    p = argparse.ArgumentParser(description="CONFI Scalper — бэктест")
    p.add_argument("--config",    default="config.yaml")
    p.add_argument("--data",      default=None,   help="Путь к CSV со снимками")
    p.add_argument("--symbol",    default="BTCUSDT")
    p.add_argument("--synthetic", action="store_true",
                   help="Использовать синтетические данные")
    p.add_argument("--n",         type=int, default=20_000,
                   help="Число синтетических снимков")
    p.add_argument("--sweep",     action="store_true",
                   help="Перебор параметров")
    p.add_argument("--balance",   type=float, default=1_000.0,
                   help="Начальный баланс USDT")
    p.add_argument("--slip",      type=float, default=5.0,
                   help="Проскальзывание в bps")
    p.add_argument("--commission", type=float, default=0.1,
                   help="Комиссия в %%")
    p.add_argument("--output",    default=None,
                   help="Путь для сохранения результатов JSON")
    return p.parse_args()


def main():
    """Основная функция запуска бэктеста."""
    args = parse_args()

    # Загружаем конфиг
    with open(args.config) as f:
        config = yaml.safe_load(f)

    # Загружаем снимки
    if args.synthetic:
        logger.info(f"Генерация {args.n} синтетических снимков...")
        snapshots = generate_synthetic_snapshots(
            symbol=args.symbol,
            n_snapshots=args.n,
        )
    elif args.data:
        snapshots = load_snapshots_from_csv(args.data, symbol=args.symbol)
    else:
        logger.error("Укажите --data или --synthetic")
        sys.exit(1)

    engine = BacktestEngine(
        config=config,
        slippage_bps=args.slip,
        commission_pct=args.commission,
        initial_balance=args.balance,
    )

    if args.sweep:
        results = engine.parameter_sweep(snapshots)
        print("\nТОП-10 комбинаций параметров:")
        print(f"{'WT':>6} {'CT':>6} {'Trades':>7} {'WR%':>7} {'PF':>8} {'PnL':>10} {'MDD':>8}")
        print("─" * 60)
        for r in results[:10]:
            print(
                f"{r['wall_threshold']:>6.1f} "
                f"{r['confidence_threshold']:>6} "
                f"{r['total_trades']:>7} "
                f"{r['win_rate']:>7.1f} "
                f"{r['profit_factor']:>8.3f} "
                f"{r['total_pnl']:>+10.2f} "
                f"{r['max_drawdown']:>8.2f}%"
            )
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
    else:
        result = engine.run(snapshots)
        print(result.summary())
        if args.output:
            data = {
                "summary": {
                    "symbol":          result.symbol,
                    "total_trades":    result.total_trades,
                    "win_rate":        result.win_rate,
                    "profit_factor":   result.profit_factor,
                    "total_pnl":       result.total_pnl,
                    "max_drawdown":    result.max_drawdown,
                    "sharpe_ratio":    result.sharpe_ratio(),
                    "final_balance":   result.final_balance,
                },
                "equity_curve": result.equity_curve,
                "exit_stats":   result.exit_stats(),
            }
            with open(args.output, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Результаты сохранены в {args.output}")


if __name__ == "__main__":
    main()
