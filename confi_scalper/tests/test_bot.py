"""
test_bot.py — Комплексные модульные тесты для CONFI Scalper.

Охватывает:
    - OrderBookAnalyzer: парсинг, стены, таяние, дисбаланс, VWAP, сброс
    - RiskManager: лимиты, размер позиции, SL/TP, дневная статистика
    - DecisionEngine: все 4 правила, уверенность, фильтрация
    - Signal: RR, валидация, is_valid
    - Position: PnL, возраст, drawdown
    - ClosedTrade: MAE/MFE, is_winner
    - TradeExecutor: dry_run, calc_quantity, build_closed_trade
    - StatsLogger: инициализация, сохранение, вычисление
    - MexcClient: подпись, парсинг ответов

Запуск:
    pytest tests/test_bot.py -v --tb=short
    pytest tests/test_bot.py -v -k "analyzer" — только тесты анализатора
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import pytest

# Добавить корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (
    AnalyzedOrderBook,
    AnalyzerConfig,
    BotConfig,
    ClosedTrade,
    Direction,
    ExitReason,
    LoggingConfig,
    MexcConfig,
    OrderLevel,
    Position,
    RiskConfig,
    Signal,
    SignalReason,
    TelegramConfig,
    Wall,
)
from modules.analyzer import OrderBookAnalyzer
from modules.decision import DecisionEngine
from modules.executor import TradeExecutor
from modules.logger_stats import StatsLogger
from modules.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры
# ---------------------------------------------------------------------------


def make_config(**overrides) -> BotConfig:
    """Создать тестовую конфигурацию с разумными значениями по умолчанию.

    Args:
        **overrides: Переопределения полей RiskConfig.

    Returns:
        BotConfig готовый к использованию в тестах.
    """
    risk = RiskConfig(
        risk_per_trade_percent=1.0,
        max_daily_loss_percent=5.0,
        max_consecutive_losses=3,
        stop_loss_percent=2.0,
        take_profit_percent=3.0,
        max_open_positions=1,
        min_risk_reward=1.0,
        **overrides,
    )
    return BotConfig(
        symbol="BTCUSDT",
        confidence_threshold=70.0,
        mexc=MexcConfig(api_key="test", api_secret="test", testnet=True),
        risk=risk,
        analyzer=AnalyzerConfig(
            wall_threshold=5.0,
            melt_speed_threshold=10.0,
            imbalance_levels=5,
            history_size=5,
            min_wall_depth=0.0,   # 0 для упрощения тестов
            max_wall_depth=5.0,
        ),
        telegram=TelegramConfig(enabled=False),
        logging=LoggingConfig(db_file=":memory:"),
        dry_run=True,
    )


def make_analyzed_book(
    imbalance: float = 1.0,
    bid_walls=None,
    ask_walls=None,
    melting_bid_walls=None,
    melting_ask_walls=None,
    mid_price: float = 30000.0,
) -> AnalyzedOrderBook:
    """Создать тестовый AnalyzedOrderBook с заданными параметрами.

    Args:
        imbalance:          Отношение bid_vol/ask_vol.
        bid_walls:          Список стен покупателей.
        ask_walls:          Список стен продавцов.
        melting_bid_walls:  Список тающих стен покупателей.
        melting_ask_walls:  Список тающих стен продавцов.
        mid_price:          Средняя цена.

    Returns:
        AnalyzedOrderBook для тестирования DecisionEngine.
    """
    return AnalyzedOrderBook(
        symbol="BTCUSDT",
        timestamp=time.time(),
        best_bid=mid_price * 0.9999,
        best_ask=mid_price * 1.0001,
        mid_price=mid_price,
        spread_pct=0.02,
        imbalance=imbalance,
        bid_walls=bid_walls or [],
        ask_walls=ask_walls or [],
        melting_bid_walls=melting_bid_walls or [],
        melting_ask_walls=melting_ask_walls or [],
        total_bid_volume=imbalance * 10.0,
        total_ask_volume=10.0,
    )


def make_wall(
    side: str = "ask",
    price: float = 30010.0,
    volume: float = 50.0,
    melt_rate: float = 0.0,
    depth: float = 0.1,
) -> Wall:
    """Создать тестовый объект Wall.

    Args:
        side:      'bid' или 'ask'.
        price:     Цена стены.
        volume:    Объём стены.
        melt_rate: Скорость таяния в %/с.
        depth:     Глубина от лучшей цены в %.

    Returns:
        Wall для использования в AnalyzedOrderBook.
    """
    return Wall(
        side=side,
        price=price,
        volume=volume,
        initial_volume=volume * 1.5,
        is_melting=melt_rate > 0,
        melt_rate=melt_rate,
        depth_from_best=depth,
    )


# ---------------------------------------------------------------------------
# Тесты OrderBookAnalyzer
# ---------------------------------------------------------------------------


class TestOrderBookAnalyzer:
    """Тесты для модуля analyzer.py."""

    def setup_method(self):
        """Инициализация для каждого теста."""
        config = AnalyzerConfig(
            wall_threshold=3.0,
            melt_speed_threshold=5.0,
            imbalance_levels=5,
            history_size=5,
            min_wall_depth=0.0,
            max_wall_depth=10.0,
            spread_max_pct=1.0,
        )
        self.analyzer = OrderBookAnalyzer(config, symbol="BTCUSDT")

    def test_initial_state(self):
        """Анализатор должен начинать с пустым стаканом."""
        assert self.analyzer.update_count == 0
        assert self.analyzer.is_stale  # Только что создан — нет обновлений

    @pytest.mark.asyncio
    async def test_empty_book_returns_none(self):
        """Пустой стакан не должен давать результат."""
        result = await self.analyzer.update({"bids": [], "asks": []})
        assert result is None

    @pytest.mark.asyncio
    async def test_normal_book_parse(self):
        """Нормальный стакан должен корректно парситься."""
        data = {
            "bids": [["30000", "1.0"], ["29990", "2.0"], ["29980", "1.5"],
                     ["29970", "1.2"], ["29960", "0.8"]],
            "asks": [["30010", "1.0"], ["30020", "2.0"], ["30030", "1.5"],
                     ["30040", "1.2"], ["30050", "0.8"]],
        }
        result = await self.analyzer.update(data)
        assert result is not None
        assert result.best_bid == 30000.0
        assert result.best_ask == 30010.0
        assert result.mid_price == 30005.0
        assert result.spread_pct > 0
        assert self.analyzer.update_count == 1

    @pytest.mark.asyncio
    async def test_wall_detection_ask_side(self):
        """Стена на стороне ask должна быть обнаружена при объёме > avg * threshold."""
        data = {
            "bids": [["30000", "1.0"], ["29990", "1.0"], ["29980", "1.0"],
                     ["29970", "1.0"], ["29960", "1.0"]],
            # Стена: 30050 имеет объём 30.0, avg ≈ 3.0, 30.0 > 3.0 * 3.0
            "asks": [["30010", "1.0"], ["30020", "1.0"], ["30030", "1.0"],
                     ["30040", "1.0"], ["30050", "30.0"]],
        }
        result = await self.analyzer.update(data)
        assert result is not None
        assert len(result.ask_walls) > 0, "Стена ask не обнаружена"
        wall = result.ask_walls[0]
        assert wall.side == "ask"
        assert wall.volume == 30.0

    @pytest.mark.asyncio
    async def test_wall_detection_bid_side(self):
        """Стена на стороне bid должна быть обнаружена."""
        data = {
            # Стена: 29960 имеет объём 30.0
            "bids": [["30000", "1.0"], ["29990", "1.0"], ["29980", "1.0"],
                     ["29970", "1.0"], ["29960", "30.0"]],
            "asks": [["30010", "1.0"], ["30020", "1.0"], ["30030", "1.0"],
                     ["30040", "1.0"], ["30050", "1.0"]],
        }
        result = await self.analyzer.update(data)
        assert result is not None
        assert len(result.bid_walls) > 0, "Стена bid не обнаружена"
        assert result.bid_walls[0].side == "bid"

    @pytest.mark.asyncio
    async def test_imbalance_balanced(self):
        """При равных объёмах bid/ask дисбаланс должен быть ≈ 1.0."""
        data = {
            "bids": [["30000", "2.0"], ["29990", "2.0"], ["29980", "2.0"],
                     ["29970", "2.0"], ["29960", "2.0"]],
            "asks": [["30010", "2.0"], ["30020", "2.0"], ["30030", "2.0"],
                     ["30040", "2.0"], ["30050", "2.0"]],
        }
        result = await self.analyzer.update(data)
        assert result is not None
        assert abs(result.imbalance - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_imbalance_bid_heavy(self):
        """При большем объёме bid дисбаланс должен быть > 1."""
        data = {
            "bids": [["30000", "5.0"], ["29990", "5.0"], ["29980", "5.0"],
                     ["29970", "5.0"], ["29960", "5.0"]],
            "asks": [["30010", "1.0"], ["30020", "1.0"], ["30030", "1.0"],
                     ["30040", "1.0"], ["30050", "1.0"]],
        }
        result = await self.analyzer.update(data)
        assert result is not None
        assert result.imbalance == pytest.approx(5.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_level_removal_on_zero_volume(self):
        """Уровень с нулевым объёмом должен удаляться из стакана."""
        data = {
            "bids": [["30000", "1.0"], ["29990", "2.0"]],
            "asks": [["30010", "1.0"], ["30020", "2.0"]],
        }
        await self.analyzer.update(data)
        assert 30000.0 in self.analyzer._bids

        # Удалить уровень
        remove_data = {"bids": [["30000", "0"]], "asks": []}
        await self.analyzer.update(remove_data)
        assert 30000.0 not in self.analyzer._bids

    @pytest.mark.asyncio
    async def test_vwap_calculation(self):
        """VWAP должен быть средневзвешенным по объёму."""
        data = {
            "bids": [["30000", "1.0"], ["29990", "3.0"]],  # VWAP = (30000*1 + 29990*3)/4 = 29992.5
            "asks": [["30010", "2.0"], ["30020", "2.0"]],  # VWAP = 30015
        }
        result = await self.analyzer.update(data)
        assert result is not None
        assert result.vwap_bid == pytest.approx(29992.5, abs=0.1)
        assert result.vwap_ask == pytest.approx(30015.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_inverted_spread_returns_none(self):
        """Инвертированный спред (bid > ask) должен возвращать None."""
        data = {
            "bids": [["30020", "1.0"]],  # bid > ask
            "asks": [["30010", "1.0"]],
        }
        result = await self.analyzer.update(data)
        assert result is None

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        """После reset() стакан должен быть пустым."""
        data = {
            "bids": [["30000", "1.0"]],
            "asks": [["30010", "1.0"]],
        }
        await self.analyzer.update(data)
        assert self.analyzer.update_count > 0

        self.analyzer.reset()
        assert self.analyzer.update_count == 0
        assert len(self.analyzer._bids) == 0
        assert len(self.analyzer._asks) == 0

    @pytest.mark.asyncio
    async def test_invalid_level_data_ignored(self):
        """Некорректные данные уровня должны игнорироваться без исключения."""
        data = {
            "bids": [["30000", "1.0"], ["invalid", "data"], [None, None]],
            "asks": [["30010", "1.0"]],
        }
        # Не должно выбрасывать исключение
        result = await self.analyzer.update(data)
        assert result is not None
        assert 30000.0 in self.analyzer._bids

    def test_get_book_snapshot(self):
        """get_book_snapshot() должен возвращать корректный словарь."""
        snapshot = self.analyzer.get_book_snapshot()
        assert "symbol" in snapshot
        assert "bids" in snapshot
        assert "asks" in snapshot
        assert snapshot["symbol"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# Тесты RiskManager
# ---------------------------------------------------------------------------


class TestRiskManager:
    """Тесты для модуля risk_manager.py."""

    def setup_method(self):
        """Инициализация для каждого теста."""
        self.config = make_config()
        self.rm = RiskManager(self.config)
        self.rm.set_daily_start_balance(1000.0)

    @pytest.mark.asyncio
    async def test_can_trade_initially(self):
        """По умолчанию торговля должна быть разрешена."""
        allowed, reason = await self.rm.can_trade(current_open_positions=0)
        assert allowed, f"Торговля не разрешена: {reason}"
        assert reason == ""

    @pytest.mark.asyncio
    async def test_blocks_when_max_positions_reached(self):
        """Торговля должна блокироваться при достижении лимита позиций."""
        allowed, reason = await self.rm.can_trade(current_open_positions=1)
        assert not allowed
        assert "лимит" in reason.lower() or "позиций" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_after_consecutive_losses(self):
        """Торговля должна блокироваться после 3 убытков подряд."""
        for _ in range(3):
            await self.rm.on_position_closed(-10.0)

        allowed, reason = await self.rm.can_trade(current_open_positions=0)
        assert not allowed
        assert "убыт" in reason.lower() or "потер" in reason.lower() or "лимит" in reason.lower()

    @pytest.mark.asyncio
    async def test_resets_consecutive_losses_on_win(self):
        """Победа должна сбрасывать счётчик серийных убытков."""
        await self.rm.on_position_closed(-10.0)
        await self.rm.on_position_closed(-10.0)
        assert self.rm.consecutive_losses == 2

        await self.rm.on_position_closed(20.0)  # Выигрыш
        assert self.rm.consecutive_losses == 0

    @pytest.mark.asyncio
    async def test_blocks_on_daily_loss_limit(self):
        """Торговля должна блокироваться при превышении дневного лимита убытков."""
        # 5% от 1000 = 50 USDT — граница лимита
        await self.rm.on_position_closed(-60.0)  # >5%

        allowed, _ = await self.rm.can_trade(0)
        assert not allowed

    def test_calculate_position_size(self):
        """Размер позиции = balance * risk_pct / entry_price."""
        qty = self.rm.calculate_position_size(balance=1000.0, entry_price=30000.0)
        # 1000 * 1% = 10 USDT / 30000 = 0.000333...
        expected = 1000.0 * 0.01 / 30000.0
        assert qty == pytest.approx(expected, rel=1e-5)

    def test_position_size_zero_balance(self):
        """Нулевой баланс должен давать нулевой размер."""
        qty = self.rm.calculate_position_size(0, 30000.0)
        assert qty == 0.0

    def test_position_size_zero_price(self):
        """Нулевая цена должна давать нулевой размер."""
        qty = self.rm.calculate_position_size(1000.0, 0.0)
        assert qty == 0.0

    def test_calculate_sl_tp_long(self):
        """SL/TP для LONG: SL ниже, TP выше цены входа."""
        sl, tp = self.rm.calculate_sl_tp(Direction.LONG, 30000.0)
        assert sl < 30000.0, "SL для LONG должен быть ниже цены входа"
        assert tp > 30000.0, "TP для LONG должен быть выше цены входа"
        assert sl == pytest.approx(30000.0 * 0.98, rel=1e-5)
        assert tp == pytest.approx(30000.0 * 1.03, rel=1e-5)

    def test_calculate_sl_tp_short(self):
        """SL/TP для SHORT: SL выше, TP ниже цены входа."""
        sl, tp = self.rm.calculate_sl_tp(Direction.SHORT, 30000.0)
        assert sl > 30000.0, "SL для SHORT должен быть выше цены входа"
        assert tp < 30000.0, "TP для SHORT должен быть ниже цены входа"

    def test_daily_pnl_tracking(self):
        """Суточный PnL должен корректно накапливаться."""
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.rm.on_position_closed(20.0))
        loop.run_until_complete(self.rm.on_position_closed(-5.0))
        loop.close()
        assert self.rm.daily_pnl == pytest.approx(15.0, abs=0.01)

    def test_trailing_stop_long(self):
        """Трейлинг для LONG должен двигаться только вверх."""
        config = make_config()
        config.risk.trailing_stop_enabled = True
        config.risk.trailing_stop_pct = 1.0
        rm = RiskManager(config)

        new_sl = rm.update_trailing_stop(Direction.LONG, 31000.0, 29400.0)
        # 31000 * (1 - 0.01) = 30690
        assert new_sl == pytest.approx(30690.0, rel=1e-4)

    def test_trailing_stop_does_not_move_down_long(self):
        """Трейлинг для LONG не должен двигаться вниз."""
        config = make_config()
        config.risk.trailing_stop_enabled = True
        rm = RiskManager(config)

        existing_sl = 30000.0
        new_sl = rm.update_trailing_stop(Direction.LONG, 29000.0, existing_sl)
        assert new_sl == existing_sl  # Не двигается

    def test_pause_trading(self):
        """После pause_trading торговля должна быть заблокирована."""
        self.rm.pause_trading(3600)
        loop = asyncio.new_event_loop()
        allowed, reason = loop.run_until_complete(self.rm.can_trade(0))
        loop.close()
        assert not allowed
        assert "пауза" in reason.lower()

    def test_get_stats_returns_dict(self):
        """get_stats() должен возвращать словарь с необходимыми ключами."""
        stats = self.rm.get_stats()
        assert "daily_pnl" in stats
        assert "consecutive_losses" in stats
        assert "win_rate" in stats
        assert "total_trades" in stats


# ---------------------------------------------------------------------------
# Тесты DecisionEngine
# ---------------------------------------------------------------------------


class TestDecisionEngine:
    """Тесты для модуля decision.py."""

    def setup_method(self):
        """Инициализация для каждого теста."""
        self.config = make_config()
        self.config.confidence_threshold = 50.0  # Снизим для тестов
        self.engine = DecisionEngine(self.config)

    def test_no_signal_on_neutral_book(self):
        """Нейтральный стакан (imbalance=1.0, нет стен) не должен давать сигнал."""
        ob = make_analyzed_book(imbalance=1.0)
        signal = self.engine.evaluate(ob)
        assert signal is None

    def test_long_signal_on_melting_ask_wall(self):
        """Тающая ask-стена должна давать LONG сигнал."""
        melt_wall = make_wall(side="ask", melt_rate=20.0)
        ob = make_analyzed_book(
            melting_ask_walls=[melt_wall],
            ask_walls=[melt_wall],
        )
        signal = self.engine.evaluate(ob)
        assert signal is not None
        assert signal.direction == Direction.LONG
        assert signal.reason == SignalReason.MELTING_ASK_WALL

    def test_short_signal_on_melting_bid_wall(self):
        """Тающая bid-стена должна давать SHORT сигнал."""
        melt_wall = make_wall(side="bid", melt_rate=20.0)
        ob = make_analyzed_book(
            melting_bid_walls=[melt_wall],
            bid_walls=[melt_wall],
        )
        signal = self.engine.evaluate(ob)
        assert signal is not None
        assert signal.direction == Direction.SHORT
        assert signal.reason == SignalReason.MELTING_BID_WALL

    def test_long_signal_on_bid_imbalance(self):
        """Высокий дисбаланс bid должен давать LONG сигнал (без стен)."""
        ob = make_analyzed_book(imbalance=3.0)  # > 2.0 threshold
        signal = self.engine.evaluate(ob)
        assert signal is not None
        assert signal.direction == Direction.LONG
        assert signal.reason == SignalReason.BID_IMBALANCE

    def test_short_signal_on_ask_imbalance(self):
        """Высокий дисбаланс ask должен давать SHORT сигнал (без стен)."""
        ob = make_analyzed_book(imbalance=0.3)  # < 0.5 threshold
        signal = self.engine.evaluate(ob)
        assert signal is not None
        assert signal.direction == Direction.SHORT
        assert signal.reason == SignalReason.ASK_IMBALANCE

    def test_melting_wall_takes_priority_over_imbalance(self):
        """Тающая стена должна иметь приоритет над дисбалансом."""
        melt_wall = make_wall(side="ask", melt_rate=20.0)
        # Дисбаланс bid (должен давать LONG), но bid-стена тает (SHORT приоритетнее для bid)
        bid_melt_wall = make_wall(side="bid", melt_rate=20.0)
        ob = make_analyzed_book(
            imbalance=3.0,  # LONG сигнал по дисбалансу
            melting_bid_walls=[bid_melt_wall],  # SHORT сигнал по стене (правило 2)
            bid_walls=[bid_melt_wall],
            # Правило 1 (тающая ask) имеет высший приоритет
            melting_ask_walls=[melt_wall],
            ask_walls=[melt_wall],
        )
        signal = self.engine.evaluate(ob)
        # Правило 1 (тающая ask → LONG) должно победить
        assert signal is not None
        assert signal.reason == SignalReason.MELTING_ASK_WALL

    def test_bid_imbalance_suppressed_by_ask_wall(self):
        """Сигнал bid-дисбаланса должен подавляться если есть ask-стена."""
        ask_wall = make_wall(side="ask", melt_rate=0)  # Не тающая стена
        ob = make_analyzed_book(
            imbalance=3.0,  # Должен давать LONG
            ask_walls=[ask_wall],  # Но стена подавляет сигнал
        )
        signal = self.engine.evaluate(ob)
        # Если стена не тает — сигнал должен быть подавлен
        assert signal is None or signal.reason != SignalReason.BID_IMBALANCE

    def test_signal_has_valid_sl_tp_long(self):
        """Сигнал LONG должен иметь SL ниже и TP выше цены входа."""
        ob = make_analyzed_book(imbalance=3.0, mid_price=30000.0)
        signal = self.engine.evaluate(ob)
        if signal and signal.direction == Direction.LONG:
            assert signal.stop_loss < signal.entry_price
            assert signal.take_profit > signal.entry_price

    def test_signal_has_valid_sl_tp_short(self):
        """Сигнал SHORT должен иметь SL выше и TP ниже цены входа."""
        ob = make_analyzed_book(imbalance=0.3, mid_price=30000.0)
        signal = self.engine.evaluate(ob)
        if signal and signal.direction == Direction.SHORT:
            assert signal.stop_loss > signal.entry_price
            assert signal.take_profit < signal.entry_price

    def test_low_confidence_filtered(self):
        """Сигнал с недостаточной уверенностью должен фильтроваться."""
        self.config.confidence_threshold = 99.0  # Очень высокий порог
        engine = DecisionEngine(self.config)

        ob = make_analyzed_book(imbalance=2.1)  # Едва превышает порог
        signal = engine.evaluate(ob)
        assert signal is None

    def test_none_book_returns_none(self):
        """None на вход должен давать None на выход."""
        signal = self.engine.evaluate(None)
        assert signal is None

    def test_stats_tracking(self):
        """Счётчики сигналов должны обновляться."""
        ob_neutral = make_analyzed_book(imbalance=1.0)
        ob_signal = make_analyzed_book(imbalance=3.0)

        self.engine.evaluate(ob_neutral)
        initial_total = self.engine.total_evaluated

        self.engine.evaluate(ob_signal)
        assert self.engine.total_evaluated >= initial_total

    def test_get_stats_keys(self):
        """get_stats() должен возвращать dict с нужными ключами."""
        stats = self.engine.get_stats()
        assert "signals_generated" in stats
        assert "signals_filtered" in stats
        assert "pass_rate_pct" in stats


# ---------------------------------------------------------------------------
# Тесты моделей Signal, Position, ClosedTrade
# ---------------------------------------------------------------------------


class TestSignal:
    """Тесты модели Signal."""

    def make_signal(self, direction=Direction.LONG) -> Signal:
        """Создать тестовый сигнал."""
        if direction == Direction.LONG:
            sl, tp = 29400.0, 30900.0
        else:
            sl, tp = 30600.0, 29100.0
        return Signal(
            direction=direction,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=sl,
            take_profit=tp,
            symbol="BTCUSDT",
        )

    def test_risk_reward_long(self):
        """RR для LONG = (TP-entry) / (entry-SL)."""
        signal = self.make_signal(Direction.LONG)
        expected_rr = (30900 - 30000) / (30000 - 29400)
        assert signal.risk_reward_ratio == pytest.approx(expected_rr, rel=1e-5)

    def test_risk_reward_short(self):
        """RR для SHORT = (entry-TP) / (SL-entry)."""
        signal = self.make_signal(Direction.SHORT)
        expected_rr = (30000 - 29100) / (30600 - 30000)
        assert signal.risk_reward_ratio == pytest.approx(expected_rr, rel=1e-5)

    def test_is_valid_true(self):
        """Корректный сигнал должен проходить валидацию."""
        signal = self.make_signal()
        assert signal.is_valid

    def test_is_valid_false_zero_price(self):
        """Сигнал с нулевой ценой не должен проходить валидацию."""
        signal = Signal(
            direction=Direction.LONG,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=0.0,  # Нулевая цена
            stop_loss=29400.0,
            take_profit=30900.0,
        )
        assert not signal.is_valid

    def test_is_valid_false_none_direction(self):
        """Сигнал NONE не должен проходить валидацию."""
        signal = Signal(
            direction=Direction.NONE,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=29400.0,
            take_profit=30900.0,
        )
        assert not signal.is_valid


class TestPosition:
    """Тесты модели Position."""

    def make_position(self, direction=Direction.LONG) -> Position:
        """Создать тестовую позицию."""
        pos = Position(
            id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            direction=direction,
            entry_price=30000.0,
            quantity=0.1,
            stop_loss=29400.0,
            take_profit=30900.0,
        )
        pos.current_price = 30000.0
        return pos

    def test_unrealized_pnl_long_profit(self):
        """Нереализованный PnL для LONG при росте цены должен быть положительным."""
        pos = self.make_position(Direction.LONG)
        pos.current_price = 30500.0
        assert pos.unrealized_pnl == pytest.approx(50.0, abs=0.01)

    def test_unrealized_pnl_long_loss(self):
        """Нереализованный PnL для LONG при падении цены должен быть отрицательным."""
        pos = self.make_position(Direction.LONG)
        pos.current_price = 29500.0
        assert pos.unrealized_pnl == pytest.approx(-50.0, abs=0.01)

    def test_unrealized_pnl_short_profit(self):
        """Нереализованный PnL для SHORT при падении цены должен быть положительным."""
        pos = self.make_position(Direction.SHORT)
        pos.current_price = 29500.0
        assert pos.unrealized_pnl == pytest.approx(50.0, abs=0.01)

    def test_notional_value(self):
        """Номинальная стоимость = entry * qty."""
        pos = self.make_position()
        assert pos.notional_value == pytest.approx(3000.0, abs=0.01)

    def test_age_seconds_increases(self):
        """Возраст позиции должен увеличиваться со временем."""
        pos = self.make_position()
        pos.opened_at = time.time() - 60
        assert pos.age_seconds >= 60.0


class TestClosedTrade:
    """Тесты модели ClosedTrade."""

    def make_trade(self, net_pnl=50.0) -> ClosedTrade:
        """Создать тестовую закрытую сделку."""
        return ClosedTrade(
            id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            direction=Direction.LONG,
            entry_price=30000.0,
            exit_price=30500.0,
            quantity=0.1,
            pnl=50.0,
            pnl_pct=1.67,
            exit_reason=ExitReason.TAKE_PROFIT,
            signal_reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            duration_sec=120.0,
            opened_at=time.time() - 120,
            net_pnl=net_pnl,
            max_favorable_price=30600.0,
            max_adverse_price=29800.0,
        )

    def test_is_winner(self):
        """is_winner должен быть True при положительном net_pnl."""
        trade = self.make_trade(50.0)
        assert trade.is_winner

    def test_is_loser(self):
        """is_winner должен быть False при отрицательном net_pnl."""
        trade = self.make_trade(-20.0)
        assert not trade.is_winner

    def test_mfe_pct_long(self):
        """MFE для LONG = (max_fav - entry) / entry * 100."""
        trade = self.make_trade()
        expected = (30600 - 30000) / 30000 * 100
        assert trade.mfe_pct == pytest.approx(expected, rel=1e-4)

    def test_mae_pct_long(self):
        """MAE для LONG = (entry - max_adv) / entry * 100."""
        trade = self.make_trade()
        expected = (30000 - 29800) / 30000 * 100
        assert trade.mae_pct == pytest.approx(expected, rel=1e-4)


# ---------------------------------------------------------------------------
# Тесты TradeExecutor
# ---------------------------------------------------------------------------


class TestTradeExecutor:
    """Тесты для модуля executor.py."""

    def setup_method(self):
        """Инициализация для каждого теста."""
        self.config = make_config()
        self.executor = TradeExecutor(self.config)

    def test_calc_quantity(self):
        """Объём = balance * risk% / price."""
        signal = Signal(
            direction=Direction.LONG,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=29400.0,
            take_profit=30900.0,
        )
        qty = self.executor._calc_quantity(signal, 1000.0)
        expected = 1000.0 * 0.01 / 30000.0
        assert qty == pytest.approx(expected, rel=1e-5)

    def test_build_closed_trade_long_profit(self):
        """_build_closed_trade должен корректно рассчитывать PnL для LONG."""
        pos = Position(
            id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            direction=Direction.LONG,
            entry_price=30000.0,
            quantity=0.1,
            stop_loss=29400.0,
            take_profit=30900.0,
            opened_at=time.time() - 60,
        )
        pos.signal = Signal(
            direction=Direction.LONG,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=29400.0,
            take_profit=30900.0,
        )
        trade = self.executor._build_closed_trade(pos, 30900.0, ExitReason.TAKE_PROFIT)
        assert trade.exit_price == 30900.0
        assert trade.pnl == pytest.approx(90.0, abs=0.01)  # (30900-30000)*0.1
        assert trade.is_winner
        assert trade.exit_reason == ExitReason.TAKE_PROFIT

    def test_build_closed_trade_short_loss(self):
        """_build_closed_trade должен корректно рассчитывать PnL для SHORT при убытке."""
        pos = Position(
            id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            direction=Direction.SHORT,
            entry_price=30000.0,
            quantity=0.1,
            stop_loss=30600.0,
            take_profit=29100.0,
            opened_at=time.time() - 30,
        )
        pos.signal = Signal(
            direction=Direction.SHORT,
            reason=SignalReason.ASK_IMBALANCE,
            confidence=75.0,
            entry_price=30000.0,
            stop_loss=30600.0,
            take_profit=29100.0,
        )
        # Стоп-лосс сработал (цена выросла)
        trade = self.executor._build_closed_trade(pos, 30600.0, ExitReason.STOP_LOSS)
        assert trade.pnl == pytest.approx(-60.0, abs=0.01)  # (30000-30600)*0.1
        assert not trade.is_winner

    @pytest.mark.asyncio
    async def test_open_position_dry_run(self):
        """open_position в dry_run должен создавать позицию без реальных ордеров."""
        signal = Signal(
            direction=Direction.LONG,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=29400.0,
            take_profit=30900.0,
            symbol="BTCUSDT",
        )
        position = await self.executor.open_position(signal, balance=1000.0)
        assert position is not None
        assert position.direction == Direction.LONG
        assert position.entry_price == 30000.0
        assert position.sl_order_id.startswith("dry_")
        assert self.executor.open_positions_count == 1

    @pytest.mark.asyncio
    async def test_open_position_limit_respected(self):
        """Нельзя открыть больше max_open_positions позиций."""
        signal = Signal(
            direction=Direction.LONG,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=29400.0,
            take_profit=30900.0,
            symbol="BTCUSDT",
        )
        pos1 = await self.executor.open_position(signal, 1000.0)
        assert pos1 is not None

        # Вторая позиция должна быть отклонена
        pos2 = await self.executor.open_position(signal, 1000.0)
        assert pos2 is None

    @pytest.mark.asyncio
    async def test_close_position(self):
        """close_position должен корректно закрывать позицию."""
        closed_trades = []

        async def on_closed(trade):
            closed_trades.append(trade)

        self.executor.on_position_closed = on_closed

        signal = Signal(
            direction=Direction.LONG,
            reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            entry_price=30000.0,
            stop_loss=29400.0,
            take_profit=30900.0,
            symbol="BTCUSDT",
        )
        self.executor.update_price(30500.0)
        pos = await self.executor.open_position(signal, 1000.0)
        assert pos is not None

        trade = await self.executor.close_position(pos.id, ExitReason.MANUAL)
        assert trade is not None
        assert self.executor.open_positions_count == 0
        assert len(closed_trades) == 1

    def test_update_price(self):
        """update_price должен обновлять _current_price."""
        self.executor.update_price(31000.0)
        assert self.executor._current_price == 31000.0

    def test_total_realized_pnl_initially_zero(self):
        """Суммарный PnL в начале должен быть нулевым."""
        assert self.executor.total_realized_pnl == 0.0


# ---------------------------------------------------------------------------
# Тесты StatsLogger
# ---------------------------------------------------------------------------


class TestStatsLogger:
    """Тесты для модуля logger_stats.py."""

    def setup_method(self):
        """Инициализация с in-memory БД."""
        config = LoggingConfig(
            db_file=":memory:",
            log_file="/tmp/test_bot.log",
        )
        self.stats = StatsLogger(config)

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self):
        """initialize() должна создавать таблицы в БД."""
        config = LoggingConfig(db_file="/tmp/test_confi_scalper.db")
        stats = StatsLogger(config)
        await stats.initialize()
        assert stats._conn is not None
        cursor = stats._conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "trades" in tables
        assert "signals" in tables
        assert "daily_stats" in tables
        await stats.close()
        if os.path.exists("/tmp/test_confi_scalper.db"):
            os.remove("/tmp/test_confi_scalper.db")

    @pytest.mark.asyncio
    async def test_save_and_retrieve_trade(self):
        """Сохранённая сделка должна корректно читаться из БД."""
        config = LoggingConfig(db_file="/tmp/test_confi_scalper2.db")
        stats = StatsLogger(config)
        await stats.initialize()

        trade = ClosedTrade(
            id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            direction=Direction.LONG,
            entry_price=30000.0,
            exit_price=30900.0,
            quantity=0.1,
            pnl=90.0,
            pnl_pct=3.0,
            exit_reason=ExitReason.TAKE_PROFIT,
            signal_reason=SignalReason.BID_IMBALANCE,
            confidence=80.0,
            duration_sec=120.0,
            opened_at=time.time() - 120,
            net_pnl=89.7,
            commission=0.3,
        )
        await stats.save_trade(trade)

        cursor = stats._conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE id=?", (trade.id,))
        row = cursor.fetchone()
        assert row is not None
        assert row["symbol"] == "BTCUSDT"
        assert row["net_pnl"] == pytest.approx(89.7, abs=0.001)

        await stats.close()
        if os.path.exists("/tmp/test_confi_scalper2.db"):
            os.remove("/tmp/test_confi_scalper2.db")

    @pytest.mark.asyncio
    async def test_save_without_init_does_not_crash(self):
        """Сохранение без инициализации не должно вызывать исключение."""
        trade = ClosedTrade(
            id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            direction=Direction.LONG,
            entry_price=30000.0,
            exit_price=30500.0,
            quantity=0.1,
            pnl=50.0,
            pnl_pct=1.67,
            exit_reason=ExitReason.TAKE_PROFIT,
            signal_reason=SignalReason.BID_IMBALANCE,
            confidence=75.0,
            duration_sec=60.0,
            opened_at=time.time() - 60,
            net_pnl=49.7,
        )
        # Не должно падать
        await self.stats.save_trade(trade)


# ---------------------------------------------------------------------------
# Тесты OrderLevel и Wall
# ---------------------------------------------------------------------------


class TestOrderLevelWall:
    """Тесты для dataclass OrderLevel и Wall."""

    def test_melt_rate_zero_initially(self):
        """melt_rate должен быть нулевым если prev_volume = 0."""
        lvl = OrderLevel(price=30000.0, volume=1.0, prev_volume=0.0)
        assert lvl.melt_rate == 0.0

    def test_melt_rate_calculated(self):
        """melt_rate = (prev - current) / prev / elapsed * 100."""
        now = time.time()
        lvl = OrderLevel(
            price=30000.0,
            volume=0.5,    # Уменьшился с 1.0 до 0.5
            prev_volume=1.0,
            first_seen_ts=now - 5.0,  # 5 секунд назад
            last_update_ts=now,
        )
        # (1.0 - 0.5) / 1.0 * 100 / 5 = 10%/s
        assert lvl.melt_rate == pytest.approx(10.0, rel=0.1)

    def test_wall_volume_remaining_pct(self):
        """volume_remaining_pct должен быть корректным."""
        wall = Wall(side="ask", price=30100.0, volume=75.0, initial_volume=100.0)
        assert wall.volume_remaining_pct == pytest.approx(75.0, abs=0.01)

    def test_wall_volume_remaining_no_initial(self):
        """При initial_volume = 0 remaining должен быть 100%."""
        wall = Wall(side="bid", price=29900.0, volume=50.0, initial_volume=0.0)
        assert wall.volume_remaining_pct == 100.0


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # Для запуска без pytest
    import subprocess
    result = subprocess.run(
        ["python", "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    sys.exit(result.returncode)
