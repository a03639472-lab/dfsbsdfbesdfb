"""
decision.py — Модуль принятия торговых решений (Decision Engine).

Отвечает за генерацию торговых сигналов на основе анализа стакана ордеров.
Реализует 4 базовых правила по приоритету:

    Правило 1 (LONG): Тающая ask-стена → покупатели «убирают» продавцов.
    Правило 2 (SHORT): Тающая bid-стена → продавцы «убирают» покупателей.
    Правило 3 (LONG): Значительный дисбаланс в сторону bid при отсутствии стен.
    Правило 4 (SHORT): Значительный дисбаланс в сторону ask при отсутствии стен.

Уверенность (confidence):
    Рассчитывается для каждого правила и нормализуется к [0..100].
    Сигнал генерируется только если confidence >= config.confidence_threshold.

Соотношение риск/доходность:
    Сигнал отфильтровывается если risk_reward_ratio < config.min_risk_reward.

Использование:
    engine = DecisionEngine(config)
    signal = engine.evaluate(analyzed_order_book)
    if signal:
        await executor.open_position(signal, balance)
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from models import (
    AnalyzedOrderBook,
    AnalyzerConfig,
    BotConfig,
    Direction,
    RiskConfig,
    Signal,
    SignalReason,
    Wall,
)

logger = logging.getLogger(__name__)


class DecisionEngine:
    """Движок принятия торговых решений на основе анализа стакана.

    Оценивает состояние стакана и генерирует торговый сигнал с уровнями
    входа, стоп-лосса и тейк-профита.

    Attributes:
        config:       Главная конфигурация бота.
        _signal_count: Статистика: сколько сигналов сгенерировано.
        _filtered_count: Статистика: сколько сигналов отфильтровано.
    """

    # Пороги дисбаланса для генерации сигналов (при отсутствии явных стен)
    IMBALANCE_LONG_THRESHOLD = 2.0   # bid_vol / ask_vol > 2.0 → LONG
    IMBALANCE_SHORT_THRESHOLD = 0.5  # bid_vol / ask_vol < 0.5 → SHORT

    def __init__(self, config: BotConfig) -> None:
        """Инициализация движка принятия решений.

        Args:
            config: Главная конфигурация бота (используются поля
                    confidence_threshold, risk.stop_loss_percent, risk.take_profit_percent).
        """
        self.config = config
        self._signal_count = 0
        self._filtered_count = 0
        logger.debug(
            f"DecisionEngine инициализирован: confidence_thr={config.confidence_threshold}, "
            f"sl={config.risk.stop_loss_percent}%, tp={config.risk.take_profit_percent}%"
        )

    def evaluate(self, ob: AnalyzedOrderBook) -> Optional[Signal]:
        """Оценить состояние стакана и сгенерировать сигнал.

        Применяет правила по приоритету. Возвращает первый подходящий сигнал
        с уверенностью >= confidence_threshold.

        Args:
            ob: Проанализированный стакан (из OrderBookAnalyzer).

        Returns:
            Signal если найден достаточно уверенный сигнал, иначе None.
        """
        if ob is None:
            return None

        # Правила по убыванию приоритета
        rules = [
            self._rule_melting_ask_wall,
            self._rule_melting_bid_wall,
            self._rule_bid_imbalance,
            self._rule_ask_imbalance,
        ]

        for rule in rules:
            try:
                result = rule(ob)
                if result is None:
                    continue
                direction, reason, confidence, metadata = result

                # Проверка порога уверенности
                if confidence < self.config.confidence_threshold:
                    logger.debug(
                        f"Сигнал {reason.value} отфильтрован: "
                        f"confidence={confidence:.1f} < thr={self.config.confidence_threshold}"
                    )
                    self._filtered_count += 1
                    continue

                # Рассчитать SL/TP
                sl, tp = self._calc_sl_tp(direction, ob.mid_price)

                signal = Signal(
                    direction=direction,
                    reason=reason,
                    confidence=confidence,
                    entry_price=ob.mid_price,
                    stop_loss=sl,
                    take_profit=tp,
                    timestamp=ob.timestamp,
                    symbol=ob.symbol,
                    metadata={
                        "imbalance": ob.imbalance,
                        "spread_pct": ob.spread_pct,
                        "bid_walls": len(ob.bid_walls),
                        "ask_walls": len(ob.ask_walls),
                        "mid_price": ob.mid_price,
                        **metadata,
                    },
                )

                # Проверка минимального RR
                if signal.risk_reward_ratio < self.config.risk.min_risk_reward:
                    logger.debug(
                        f"Сигнал {reason.value} отфильтрован: "
                        f"RR={signal.risk_reward_ratio:.2f} < min={self.config.risk.min_risk_reward}"
                    )
                    self._filtered_count += 1
                    continue

                self._signal_count += 1
                logger.info(
                    f"[{ob.symbol}] Сигнал: {signal.direction.value} | "
                    f"reason={reason.value} | conf={confidence:.1f}% | "
                    f"entry={ob.mid_price:.4f} | sl={sl:.4f} | tp={tp:.4f} | "
                    f"rr={signal.risk_reward_ratio:.2f}"
                )
                return signal

            except Exception as exc:
                logger.error(f"Ошибка в правиле {rule.__name__}: {exc}", exc_info=True)

        return None

    # ------------------------------------------------------------------
    # Торговые правила
    # ------------------------------------------------------------------

    def _rule_melting_ask_wall(
        self, ob: AnalyzedOrderBook
    ) -> Optional[Tuple[Direction, SignalReason, float, dict]]:
        """Правило 1 (приоритет): Тающая стена продавцов (ask) → LONG.

        Интерпретация: крупный продавец убирает свои ордера. Это освобождает
        путь для роста цены. Бычий сигнал с высоким приоритетом.

        Уверенность рассчитывается из:
            - Скорости таяния стены (melt_rate).
            - Объёма стены как кратного среднего.
            - Дополнительно усиливается bid-дисбалансом.

        Args:
            ob: Проанализированный стакан.

        Returns:
            (LONG, SignalReason, confidence, metadata) или None.
        """
        if not ob.has_melting_ask_wall:
            return None

        # Берём наибыстрее тающую ask-стену
        wall = max(ob.melting_ask_walls, key=lambda w: w.melt_rate)

        # Базовая уверенность от скорости таяния (нормализуем к 100)
        melt_confidence = min(wall.melt_rate / self.config.analyzer.melt_speed_threshold * 60.0, 80.0)

        # Бонус от дисбаланса bid/ask
        imbalance_bonus = min((ob.imbalance - 1.0) * 10.0, 15.0) if ob.imbalance > 1.0 else 0.0

        # Бонус от глубины стены от лучшей цены (ближе = сильнее)
        depth_bonus = max(5.0 - wall.depth_from_best * 2.0, 0.0)

        confidence = min(melt_confidence + imbalance_bonus + depth_bonus, 100.0)

        metadata = {
            "wall_price": wall.price,
            "wall_volume": wall.volume,
            "wall_melt_rate": wall.melt_rate,
            "wall_depth_pct": wall.depth_from_best,
        }
        return Direction.LONG, SignalReason.MELTING_ASK_WALL, confidence, metadata

    def _rule_melting_bid_wall(
        self, ob: AnalyzedOrderBook
    ) -> Optional[Tuple[Direction, SignalReason, float, dict]]:
        """Правило 2: Тающая стена покупателей (bid) → SHORT.

        Интерпретация: крупный покупатель убирает поддержку.
        Цена теряет опору снизу. Медвежий сигнал.

        Args:
            ob: Проанализированный стакан.

        Returns:
            (SHORT, SignalReason, confidence, metadata) или None.
        """
        if not ob.has_melting_bid_wall:
            return None

        wall = max(ob.melting_bid_walls, key=lambda w: w.melt_rate)

        melt_confidence = min(wall.melt_rate / self.config.analyzer.melt_speed_threshold * 60.0, 80.0)
        imbalance_bonus = min((1.0 / ob.imbalance - 1.0) * 10.0, 15.0) if ob.imbalance < 1.0 else 0.0
        depth_bonus = max(5.0 - wall.depth_from_best * 2.0, 0.0)

        confidence = min(melt_confidence + imbalance_bonus + depth_bonus, 100.0)

        metadata = {
            "wall_price": wall.price,
            "wall_volume": wall.volume,
            "wall_melt_rate": wall.melt_rate,
            "wall_depth_pct": wall.depth_from_best,
        }
        return Direction.SHORT, SignalReason.MELTING_BID_WALL, confidence, metadata

    def _rule_bid_imbalance(
        self, ob: AnalyzedOrderBook
    ) -> Optional[Tuple[Direction, SignalReason, float, dict]]:
        """Правило 3: Значительный дисбаланс в пользу покупателей → LONG.

        Срабатывает только при отсутствии явных стен (чтобы избежать
        ложных сигналов когда стена специально создаёт видимость дисбаланса).

        Args:
            ob: Проанализированный стакан.

        Returns:
            (LONG, SignalReason, confidence, metadata) или None.
        """
        if ob.imbalance < self.IMBALANCE_LONG_THRESHOLD:
            return None
        # Подавить сигнал если есть явная ask-стена (возможный fake wall)
        if ob.has_ask_wall:
            return None

        # Уверенность: линейно растёт с дисбалансом
        raw_conf = (ob.imbalance - self.IMBALANCE_LONG_THRESHOLD) * 15.0 + 50.0
        confidence = min(raw_conf, 90.0)

        metadata = {"imbalance_ratio": ob.imbalance}
        return Direction.LONG, SignalReason.BID_IMBALANCE, confidence, metadata

    def _rule_ask_imbalance(
        self, ob: AnalyzedOrderBook
    ) -> Optional[Tuple[Direction, SignalReason, float, dict]]:
        """Правило 4: Значительный дисбаланс в пользу продавцов → SHORT.

        Срабатывает только при отсутствии явных стен.

        Args:
            ob: Проанализированный стакан.

        Returns:
            (SHORT, SignalReason, confidence, metadata) или None.
        """
        if ob.imbalance > self.IMBALANCE_SHORT_THRESHOLD:
            return None
        if ob.has_bid_wall:
            return None

        raw_conf = (self.IMBALANCE_SHORT_THRESHOLD - ob.imbalance) / self.IMBALANCE_SHORT_THRESHOLD * 30.0 + 50.0
        confidence = min(raw_conf, 90.0)

        metadata = {"imbalance_ratio": ob.imbalance}
        return Direction.SHORT, SignalReason.ASK_IMBALANCE, confidence, metadata

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    def _calc_sl_tp(
        self, direction: Direction, price: float
    ) -> Tuple[float, float]:
        """Рассчитать уровни стоп-лосса и тейк-профита.

        Уровни рассчитываются как % отклонение от цены входа.
        Процентные ставки берутся из risk конфига.

        Args:
            direction: Направление позиции (LONG или SHORT).
            price:     Цена входа.

        Returns:
            Кортеж (stop_loss, take_profit).

        Raises:
            ValueError: Если direction == NONE (не должно происходить).
        """
        sl_pct = self.config.risk.stop_loss_percent / 100.0
        tp_pct = self.config.risk.take_profit_percent / 100.0

        if direction == Direction.LONG:
            stop_loss = price * (1.0 - sl_pct)
            take_profit = price * (1.0 + tp_pct)
        elif direction == Direction.SHORT:
            stop_loss = price * (1.0 + sl_pct)
            take_profit = price * (1.0 - tp_pct)
        else:
            raise ValueError(f"Невалидное направление для расчёта SL/TP: {direction}")

        return round(stop_loss, 8), round(take_profit, 8)

    @property
    def signal_count(self) -> int:
        """Количество сигналов, прошедших все фильтры."""
        return self._signal_count

    @property
    def filtered_count(self) -> int:
        """Количество сигналов, отфильтрованных (не прошли порог или RR)."""
        return self._filtered_count

    @property
    def total_evaluated(self) -> int:
        """Суммарное количество обработанных потенциальных сигналов."""
        return self._signal_count + self._filtered_count

    def get_stats(self) -> dict:
        """Получить статистику работы движка в виде словаря.

        Returns:
            dict с ключами: signals_generated, signals_filtered,
            total_evaluated, pass_rate_pct.
        """
        pass_rate = (
            self._signal_count / self.total_evaluated * 100
            if self.total_evaluated > 0
            else 0.0
        )
        return {
            "signals_generated": self._signal_count,
            "signals_filtered": self._filtered_count,
            "total_evaluated": self.total_evaluated,
            "pass_rate_pct": pass_rate,
        }

    def __repr__(self) -> str:
        return (f"DecisionEngine(conf_thr={self.config.confidence_threshold}, "
                f"generated={self._signal_count}, filtered={self._filtered_count})")
