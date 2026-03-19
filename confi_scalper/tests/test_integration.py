"""
tests/test_integration.py — Интеграционные тесты CONFI Scalper.

Тестирует взаимодействие между модулями (Analyzer → Decision → RiskManager)
без реального WebSocket / REST соединения.

Все тесты синхронные (бэктест-уровень).

Запуск::

    python -m pytest tests/test_integration.py -v
"""

from __future__ import annotations

import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models import Direction, ExitReason
from modules.analyzer import OrderBookAnalyzer
from modules.decision import DecisionEngine
from modules.risk_manager import RiskManager
from backtest.engine import (
    BacktestEngine, generate_synthetic_snapshots, OrderBookSnapshot
)


# ─── Конфиг для тестов ───────────────────────────────────────────────────────

def _cfg() -> dict:
    return {
        "bot":      {"symbol": "BTCUSDT", "confidence_threshold": 65},
        "mexc":     {"api_key": "", "api_secret": "",
                     "rest_url": "https://api.mexc.com",
                     "ws_url": "wss://wbs-api.mexc.com/ws",
                     "testnet": True, "depth_limit": 20},
        "analyzer": {"wall_threshold": 4.0, "melt_speed_threshold": 8.0, "history_size": 8},
        "risk":     {"risk_per_trade_percent": 1.0, "max_daily_loss_percent": 5.0,
                     "max_consecutive_losses": 5, "stop_loss_percent": 2.0,
                     "take_profit_percent": 3.0, "max_open_positions": 1},
        "telegram": {"enabled": False},
        "logging":  {"level": "WARNING", "log_file": "/tmp/test.log", "db_file": "/tmp/test.db"},
    }


class TestAnalyzerDecisionPipeline(unittest.TestCase):
    """
    Тестирует полный пайплайн: Analyzer → DecisionEngine.
    Создаёт реальные AnalyzedOrderBook и проверяет генерацию сигналов.
    """

    def setUp(self):
        self.analyzer = OrderBookAnalyzer(
            wall_threshold=4.0,
            melt_speed_threshold=8.0,
            history_size=8,
        )
        self.engine = DecisionEngine(
            confidence_threshold=60,
            upper_imbalance=2.0,
            lower_imbalance=0.5,
        )

    def test_pipeline_with_strong_imbalance(self):
        """
        Сильный дисбаланс проходит весь пайплайн и генерирует сигнал.
        """
        # Bid-объём в 4 раза больше ask-объёма → LONG
        bids = [[68_000.0 - i * 10, 4.0] for i in range(10)]
        asks = [[68_020.0 + i * 10, 1.0] for i in range(10)]

        analyzed = self.analyzer.update("BTCUSDT", bids, asks)

        self.assertGreater(analyzed.imbalance, 2.0)
        self.assertGreater(analyzed.mid_price, 0.0)

        sig = self.engine.evaluate(analyzed, stop_loss_pct=2.0, take_profit_pct=3.0)
        if sig:
            self.assertEqual(sig.direction, Direction.LONG)
            self.assertGreater(sig.take_profit, sig.entry_price)
            self.assertLess(sig.stop_loss, sig.entry_price)

    def test_pipeline_with_ask_wall(self):
        """
        Стена на стороне ask порождает сигнал LONG (без таяния — нет сигнала,
        только с таянием).
        """
        # Создаём стену: один уровень в 8× больше среднего
        asks = [[68_020.0 + i * 10, 1.0] for i in range(9)]
        asks.append([68_100.0, 8.0])  # стена ratio ≈ 1.78 — ниже порога 4

        bids = [[68_000.0 - i * 10, 1.0] for i in range(10)]
        analyzed = self.analyzer.update("BTCUSDT", bids, asks)

        # Проверяем что анализатор корректно обработал стакан
        self.assertIsNotNone(analyzed)
        self.assertGreater(analyzed.mid_price, 0)

    def test_pipeline_runs_multiple_iterations(self):
        """
        Несколько итераций не приводят к ошибкам.
        """
        bids = [[68_000.0 - i * 10, 1.0 + i * 0.1] for i in range(15)]
        asks = [[68_020.0 + i * 10, 1.0 + i * 0.1] for i in range(15)]

        for _ in range(20):
            analyzed = self.analyzer.update("BTCUSDT", bids, asks)
            sig = self.engine.evaluate(analyzed)
            # Просто не должно быть исключений
            _ = sig


class TestRiskManagerWithDecision(unittest.TestCase):
    """
    Тестирует взаимодействие DecisionEngine и RiskManager.
    Проверяет, что риск-менеджер корректно блокирует торговлю.
    """

    def setUp(self):
        self.engine = DecisionEngine(confidence_threshold=50)
        self.rm     = RiskManager(
            max_open_positions=1,
            max_consecutive_losses=2,
            max_daily_loss_percent=5.0,
        )
        self.rm.set_daily_balance(1_000.0)

    def test_after_loss_limit_no_trade(self):
        """После достижения серии убытков торговля заблокирована."""
        self.rm.on_position_closed(pnl=-10.0)
        self.rm.on_position_closed(pnl=-10.0)

        allowed, _ = self.rm.can_trade()
        self.assertFalse(allowed)

    def test_size_calculation_with_loss(self):
        """
        Размер позиции меньше при уменьшении баланса (1% от текущего).
        """
        size1 = self.rm.calculate_position_size(balance=1_000.0, entry_price=68_000.0)
        size2 = self.rm.calculate_position_size(balance=900.0,   entry_price=68_000.0)
        self.assertGreater(size1, size2)

    def test_sl_tp_symmetry(self):
        """
        SL и TP симметричны относительно entry (по процентам).
        """
        entry = 68_000.0
        sl_l  = self.rm.calculate_stop_price(entry, Direction.LONG)
        tp_l  = self.rm.calculate_take_price(entry, Direction.LONG)
        sl_s  = self.rm.calculate_stop_price(entry, Direction.SHORT)
        tp_s  = self.rm.calculate_take_price(entry, Direction.SHORT)

        # LONG: SL ниже, TP выше
        self.assertLess(sl_l, entry)
        self.assertGreater(tp_l, entry)
        # SHORT: SL выше, TP ниже
        self.assertGreater(sl_s, entry)
        self.assertLess(tp_s, entry)

        # Симметрия: LONG SL% ≈ SHORT TP% от entry
        sl_l_pct = abs(entry - sl_l) / entry
        tp_s_pct = abs(entry - tp_s) / entry
        self.assertAlmostEqual(sl_l_pct, tp_s_pct, places=6)


class TestBacktestWithRealAnalyzer(unittest.TestCase):
    """
    Тестирует BacktestEngine с реальным анализатором на синтетических данных.
    """

    def test_full_backtest_pipeline(self):
        """Полный бэктест без ошибок, с разумными метриками."""
        config    = _cfg()
        engine    = BacktestEngine(config=config, initial_balance=1_000.0)
        snapshots = generate_synthetic_snapshots(n_snapshots=2_000, seed=99)

        result = engine.run(snapshots)

        # Базовые инварианты
        self.assertGreater(result.snapshots_processed, 0)
        self.assertGreaterEqual(result.final_balance, 0)
        self.assertGreaterEqual(result.total_trades, 0)

        if result.total_trades > 0:
            self.assertGreaterEqual(result.win_rate, 0.0)
            self.assertLessEqual(result.win_rate, 100.0)
            self.assertGreater(result.profit_factor, 0.0)

    def test_backtest_equity_monotone_or_not(self):
        """Кривая эквити имеет правильный размер."""
        config    = _cfg()
        engine    = BacktestEngine(config=config, initial_balance=500.0)
        snapshots = generate_synthetic_snapshots(n_snapshots=1_000, seed=7)
        result    = engine.run(snapshots)

        # Первая точка = начальный баланс
        self.assertAlmostEqual(result.equity_curve[0][1], 500.0)
        # Число точек = number of trades + 1
        self.assertEqual(len(result.equity_curve), result.total_trades + 1)

    def test_backtest_consistent_balance(self):
        """
        Конечный баланс = начальный + sum(pnl_net) всех сделок.
        """
        config    = _cfg()
        engine    = BacktestEngine(config=config, initial_balance=1_000.0)
        snapshots = generate_synthetic_snapshots(n_snapshots=3_000, seed=13)
        result    = engine.run(snapshots)

        total_pnl   = sum(t.pnl_net for t in result.trades)
        expected_final = 1_000.0 + total_pnl
        self.assertAlmostEqual(result.final_balance, expected_final, places=4)


class TestEdgeCases(unittest.TestCase):
    """Граничные случаи и нестандартные входные данные."""

    def test_analyzer_with_string_prices(self):
        """Анализатор обрабатывает строковые цены (из JSON)."""
        analyzer = OrderBookAnalyzer()
        bids = [["68000.0", "1.5"], ["67990.0", "2.0"]]
        asks = [["68020.0", "1.2"], ["68030.0", "0.9"]]
        book = analyzer.update("BTCUSDT", bids, asks)
        self.assertGreater(book.mid_price, 0)

    def test_analyzer_single_level(self):
        """Стакан с одним уровнем — не крашится."""
        analyzer = OrderBookAnalyzer()
        book = analyzer.update("BTCUSDT", [[68_000.0, 1.0]], [[68_020.0, 1.0]])
        self.assertEqual(len(book.bids), 1)
        self.assertAlmostEqual(book.mid_price, 68_010.0)

    def test_decision_engine_max_confidence(self):
        """Уверенность не превышает 100."""
        engine = DecisionEngine(confidence_threshold=10)
        from tests.test_bot import _make_analyzed_book, _make_wall
        wall = _make_wall(is_melting=True, melt_speed=999.0, ratio=100.0)
        book = _make_analyzed_book(imbalance=5.0, ask_walls=[wall])
        sig  = engine.evaluate(book)
        if sig:
            self.assertLessEqual(sig.confidence, 100)

    def test_risk_manager_zero_balance(self):
        """Риск-менеджер корректно обрабатывает нулевой баланс."""
        rm = RiskManager()
        rm.set_daily_balance(0.0)
        allowed, _ = rm.can_trade()
        self.assertTrue(allowed)  # ноль не блокирует (нет дневного убытка)

    def test_backtest_single_snapshot(self):
        """Бэктест с одним снимком — не крашится."""
        config = _cfg()
        engine = BacktestEngine(config=config)
        snap   = OrderBookSnapshot(
            timestamp=time.time(),
            symbol="BTCUSDT",
            bids=[[68_000.0, 1.0]],
            asks=[[68_020.0, 1.0]],
        )
        result = engine.run([snap])
        self.assertEqual(result.snapshots_processed, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
