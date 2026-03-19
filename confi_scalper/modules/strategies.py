"""
modules/strategies.py — Дополнительные торговые стратегии.

Этот модуль содержит альтернативные и вспомогательные стратегии,
которые можно комбинировать с базовым DecisionEngine:

    OrderFlowStrategy   — анализ потока ордеров (order flow) на основе
                          серии сделок: большой объём покупок → LONG.

    SpreadStrategy      — торговля на сужение/расширение спреда.

    VolatilityFilter    — отфильтровывает сигналы при высокой волатильности
                          (избегаем ложных сигналов в периоды хаоса).

    MultiTimeframeFilter — проверяет согласованность сигналов на разных
                           «частотах» (быстрые/медленные обновления стакана).

    SignalAggregator    — объединяет несколько источников сигналов
                          с весовыми коэффициентами.

Все стратегии реализуют интерфейс BaseStrategy с методом evaluate().
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Optional, List, Tuple, Deque
from dataclasses import dataclass

from loguru import logger

from models import Signal, Direction, SignalReason, AnalyzedOrderBook


# ═══════════════════════════════════════════════════════════════════════════════
# Базовый интерфейс
# ═══════════════════════════════════════════════════════════════════════════════

class BaseStrategy(ABC):
    """
    Базовый класс для всех торговых стратегий.

    Каждая стратегия принимает AnalyzedOrderBook и может дополнительно
    принимать контекстные данные (последние сделки, историю цен и т.д.).
    """

    @abstractmethod
    def evaluate(self, book: AnalyzedOrderBook, **kwargs) -> Optional[Signal]:
        """
        Оценивает текущее состояние рынка и возвращает сигнал.

        Args:
            book:   Проанализированный стакан.
            kwargs: Дополнительный контекст (зависит от стратегии).

        Returns:
            Signal или None.
        """

    @property
    def name(self) -> str:
        """Имя стратегии (для логов)."""
        return self.__class__.__name__


# ═══════════════════════════════════════════════════════════════════════════════
# Order Flow Strategy
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeEvent:
    """
    Одна публичная сделка (из WebSocket deals-канала).

    Attributes:
        price:     Цена сделки.
        volume:    Объём сделки.
        side:      Сторона агрессора ('buy' или 'sell').
        timestamp: Unix-время.
    """
    price: float
    volume: float
    side: str   # 'buy' или 'sell'
    timestamp: float


class OrderFlowStrategy(BaseStrategy):
    """
    Стратегия анализа потока ордеров.

    Накапливает историю публичных сделок за окно времени.
    Генерирует сигнал, если объём покупок/продаж значительно
    превышает противоположную сторону.

    Алгоритм:
        1. Суммируем объём buy-агрессоров и sell-агрессоров за окно.
        2. Вычисляем flow_ratio = buy_vol / sell_vol.
        3. Если flow_ratio > upper_threshold → LONG (покупают активнее).
        4. Если flow_ratio < lower_threshold → SHORT.
    """

    def __init__(
        self,
        window_sec: float = 30.0,
        upper_threshold: float = 2.5,
        lower_threshold: float = 0.4,
        min_events: int = 5,
        confidence_base: int = 60,
    ):
        """
        Args:
            window_sec:       Окно времени для расчёта (секунды).
            upper_threshold:  flow_ratio > этого → LONG.
            lower_threshold:  flow_ratio < этого → SHORT.
            min_events:       Минимальное число сделок для принятия решения.
            confidence_base:  Базовая уверенность сигнала.
        """
        self.window_sec       = window_sec
        self.upper_threshold  = upper_threshold
        self.lower_threshold  = lower_threshold
        self.min_events       = min_events
        self.confidence_base  = confidence_base

        self._events: Deque[TradeEvent] = deque()

    def add_trade(self, price: float, volume: float, side: str):
        """
        Добавляет публичную сделку в историю.

        Args:
            price:  Цена сделки.
            volume: Объём.
            side:   'buy' или 'sell'.
        """
        event = TradeEvent(
            price=price,
            volume=volume,
            side=side.lower(),
            timestamp=time.time(),
        )
        self._events.append(event)
        # Очищаем устаревшие события
        self._evict_old()

    def _evict_old(self):
        """Удаляет сделки старше window_sec."""
        cutoff = time.time() - self.window_sec
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def evaluate(self, book: AnalyzedOrderBook, **kwargs) -> Optional[Signal]:
        """
        Оценивает поток ордеров и возвращает сигнал.

        Args:
            book: Проанализированный стакан (для mid_price).

        Returns:
            Signal или None.
        """
        self._evict_old()

        if len(self._events) < self.min_events:
            return None

        buy_vol  = sum(e.volume for e in self._events if e.side == "buy")
        sell_vol = sum(e.volume for e in self._events if e.side == "sell")

        if sell_vol == 0:
            return None

        flow_ratio = buy_vol / sell_vol
        direction  = None

        if flow_ratio > self.upper_threshold:
            direction = Direction.LONG
        elif flow_ratio < self.lower_threshold:
            direction = Direction.SHORT

        if direction is None:
            return None

        # Уверенность на основе flow_ratio
        if direction == Direction.LONG:
            conf = int(self.confidence_base + min(30, (flow_ratio - self.upper_threshold) * 10))
        else:
            excess = self.lower_threshold - flow_ratio
            conf   = int(self.confidence_base + min(30, excess * 30))

        conf = max(0, min(100, conf))

        return Signal(
            symbol=book.symbol,
            direction=direction,
            entry_price=book.mid_price,
            stop_loss=0.0,
            take_profit=0.0,
            confidence=conf,
            reason=SignalReason.BID_IMBALANCE if direction == Direction.LONG else SignalReason.ASK_IMBALANCE,
            extra={"flow_ratio": round(flow_ratio, 3), "events": len(self._events)},
        )

    def get_flow_stats(self) -> dict:
        """
        Возвращает текущую статистику потока ордеров.

        Returns:
            Словарь с объёмами покупок/продаж и flow_ratio.
        """
        self._evict_old()
        buy_vol  = sum(e.volume for e in self._events if e.side == "buy")
        sell_vol = sum(e.volume for e in self._events if e.side == "sell")
        return {
            "events_count": len(self._events),
            "buy_volume":   round(buy_vol, 6),
            "sell_volume":  round(sell_vol, 6),
            "flow_ratio":   round(buy_vol / sell_vol, 4) if sell_vol > 0 else 0.0,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Volatility Filter
# ═══════════════════════════════════════════════════════════════════════════════

class VolatilityFilter:
    """
    Фильтр волатильности.

    Блокирует торговые сигналы в периоды высокой волатильности,
    когда риск ложных сигналов и проскальзывания максимален.

    Алгоритм:
        Отслеживает историю mid_price. Вычисляет rolling std цен.
        Если (std / mean) * 100 > threshold_pct → высокая волатильность.

    Применение::

        vf = VolatilityFilter(threshold_pct=0.5)
        vf.update(analyzed_book.mid_price)
        if not vf.is_volatile:
            signal = engine.evaluate(book)
    """

    def __init__(
        self,
        window_size: int = 50,
        threshold_pct: float = 0.3,
    ):
        """
        Args:
            window_size:   Размер скользящего окна цен.
            threshold_pct: Порог CV (коэфф. вариации) для высокой волатильности.
        """
        self.window_size   = window_size
        self.threshold_pct = threshold_pct
        self._prices: Deque[float] = deque(maxlen=window_size)

    def update(self, mid_price: float):
        """
        Добавляет новую цену в историю.

        Args:
            mid_price: Текущая средняя цена.
        """
        if mid_price > 0:
            self._prices.append(mid_price)

    @property
    def is_volatile(self) -> bool:
        """True, если текущая волатильность превышает порог."""
        if len(self._prices) < 10:
            return False
        prices = list(self._prices)
        mean   = sum(prices) / len(prices)
        if mean == 0:
            return False
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        import math
        std = math.sqrt(variance)
        cv  = std / mean * 100
        return cv > self.threshold_pct

    @property
    def current_cv(self) -> float:
        """Текущий коэффициент вариации (%)."""
        if len(self._prices) < 2:
            return 0.0
        prices = list(self._prices)
        mean   = sum(prices) / len(prices)
        if mean == 0:
            return 0.0
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        import math
        return round(math.sqrt(variance) / mean * 100, 6)


# ═══════════════════════════════════════════════════════════════════════════════
# Spread Strategy
# ═══════════════════════════════════════════════════════════════════════════════

class SpreadAnalyzer:
    """
    Анализирует динамику спреда.

    Узкий спред → хорошая ликвидность → торговля предпочтительна.
    Расширение спреда → ликвидность падает → высокий риск.

    Предоставляет:
    - Текущий спред и его историческую норму.
    - Флаг «хорошей ликвидности».
    - Перцентиль текущего спреда относительно истории.
    """

    def __init__(self, window_size: int = 100, max_spread_pct: float = 0.05):
        """
        Args:
            window_size:    Размер истории спреда.
            max_spread_pct: Максимальный допустимый спред в % от mid_price.
        """
        self.window_size  = window_size
        self.max_spread_pct = max_spread_pct
        self._spreads: Deque[float] = deque(maxlen=window_size)

    def update(self, book: AnalyzedOrderBook):
        """
        Обновляет историю спреда.

        Args:
            book: Проанализированный стакан.
        """
        if book.spread_pct > 0:
            self._spreads.append(book.spread_pct)

    @property
    def is_liquid(self) -> bool:
        """True, если текущая ликвидность достаточна для торговли."""
        if not self._spreads:
            return True
        return self._spreads[-1] <= self.max_spread_pct

    @property
    def avg_spread_pct(self) -> float:
        """Средний спред за историю наблюдений."""
        if not self._spreads:
            return 0.0
        return sum(self._spreads) / len(self._spreads)

    def spread_percentile(self) -> float:
        """
        Перцентиль текущего спреда (0–100%).
        100% = текущий спред максимальный за историю.
        """
        if len(self._spreads) < 2:
            return 50.0
        current = self._spreads[-1]
        sorted_s = sorted(self._spreads)
        below    = sum(1 for s in sorted_s if s < current)
        return round(below / len(sorted_s) * 100, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Aggregator
# ═══════════════════════════════════════════════════════════════════════════════

class SignalAggregator:
    """
    Агрегирует сигналы от нескольких стратегий.

    Возвращает итоговый сигнал только если несколько стратегий
    согласны по направлению.

    Режимы агрегации:
        'majority'  — большинство стратегий дают одно направление.
        'unanimous' — все стратегии согласны.
        'weighted'  — взвешенное голосование.
    """

    def __init__(
        self,
        strategies: List[Tuple[BaseStrategy, float]],
        mode: str = "majority",
        min_confidence: int = 70,
    ):
        """
        Args:
            strategies:     Список пар (стратегия, вес).
            mode:           Режим агрегации ('majority', 'unanimous', 'weighted').
            min_confidence: Минимальная уверенность для итогового сигнала.
        """
        self.strategies     = strategies
        self.mode           = mode
        self.min_confidence = min_confidence

    def evaluate(
        self,
        book: AnalyzedOrderBook,
        stop_loss_pct: float = 2.0,
        take_profit_pct: float = 3.0,
    ) -> Optional[Signal]:
        """
        Получает сигналы от всех стратегий и агрегирует их.

        Args:
            book:           Проанализированный стакан.
            stop_loss_pct:  Размер SL.
            take_profit_pct: Размер TP.

        Returns:
            Агрегированный сигнал или None.
        """
        votes: List[Tuple[Optional[Signal], float]] = []
        for strategy, weight in self.strategies:
            sig = strategy.evaluate(book)
            votes.append((sig, weight))

        active = [(sig, w) for sig, w in votes if sig is not None]
        if not active:
            return None

        # Режим unanimous
        if self.mode == "unanimous" and len(active) < len(self.strategies):
            return None

        # Режим majority
        long_w  = sum(w for sig, w in active if sig.direction == Direction.LONG)
        short_w = sum(w for sig, w in active if sig.direction == Direction.SHORT)

        if long_w == 0 and short_w == 0:
            return None

        direction = Direction.LONG if long_w >= short_w else Direction.SHORT

        # В majority режиме нужно большинство по весу
        if self.mode == "majority":
            total_w = sum(w for _, w in self.strategies)
            winning_w = max(long_w, short_w)
            if winning_w < total_w / 2:
                return None

        # Средневзвешенная уверенность
        matching = [
            (sig, w) for sig, w in active if sig.direction == direction
        ]
        total_w  = sum(w for _, w in matching)
        avg_conf = int(
            sum(sig.confidence * w for sig, w in matching) / total_w
        ) if total_w > 0 else 0

        if avg_conf < self.min_confidence:
            return None

        sig = matching[0][0]
        entry = sig.entry_price

        if direction == Direction.LONG:
            stop_loss   = round(entry * (1 - stop_loss_pct / 100), 8)
            take_profit = round(entry * (1 + take_profit_pct / 100), 8)
        else:
            stop_loss   = round(entry * (1 + stop_loss_pct / 100), 8)
            take_profit = round(entry * (1 - take_profit_pct / 100), 8)

        result = Signal(
            symbol=book.symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=avg_conf,
            reason=SignalReason.COMBINED,
            extra={"aggregated": True, "sources": len(matching)},
        )

        logger.debug(
            f"SignalAggregator: {direction.value} | "
            f"conf={avg_conf} | sources={len(matching)}/{len(self.strategies)}"
        )

        return result
