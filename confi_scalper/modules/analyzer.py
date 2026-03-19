"""
analyzer.py — Модуль анализа стакана ордеров (Order Book Analyzer).

Основные функции:
    - Обновление состояния стакана из WebSocket-сообщений.
    - Обнаружение «стен» (крупных ордеров).
    - Расчёт скорости «таяния» стен (melt rate).
    - Расчёт дисбаланса bid/ask.
    - Вычисление VWAP bid и ask.
    - Расчёт спреда.
    - Формирование объекта AnalyzedOrderBook для модуля decision.py.

Алгоритм обнаружения стены:
    1. Для каждой стороны (bid/ask) вычисляется средний объём уровней.
    2. Уровень считается стеной если его объём > avg_volume * wall_threshold.
    3. Стена считается «тающей» если скорость уменьшения объёма >= melt_speed_threshold %/с.

Алгоритм расчёта дисбаланса:
    imbalance = sum(bid_vol[:K]) / sum(ask_vol[:K])
    Значение > 1 — преобладают покупатели, < 1 — продавцы.

Использование:
    analyzer = OrderBookAnalyzer(config)
    analyzed = analyzer.update(ws_depth_message)
    if analyzed:
        signal = decision_engine.evaluate(analyzed)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple, Deque

from models import (
    AnalyzedOrderBook,
    AnalyzerConfig,
    OrderLevel,
    Wall,
)

logger = logging.getLogger(__name__)


class OrderBookAnalyzer:
    """Анализатор стакана ордеров. Поддерживает живое состояние книги ордеров.

    Attributes:
        config:            Конфигурация анализатора.
        symbol:            Торговый символ (e.g. 'BTCUSDT').
        _bids:             Текущие уровни bid {price: volume}.
        _asks:             Текущие уровни ask {price: volume}.
        _bid_history:      История объёмов bid {price: deque[float]}.
        _ask_history:      История объёмов ask {price: deque[float]}.
        _bid_first_seen:   Когда впервые появился уровень bid {price: ts}.
        _ask_first_seen:   Когда впервые появился уровень ask {price: ts}.
        _last_update_ts:   Unix-timestamp последнего обновления стакана.
        _update_count:     Счётчик обновлений стакана.
        _lock:             asyncio.Lock для потокобезопасного обновления.
    """

    def __init__(self, config: AnalyzerConfig, symbol: str = "BTCUSDT") -> None:
        """Инициализация анализатора.

        Args:
            config: Конфигурация параметров анализа.
            symbol: Торговый символ.
        """
        self.config = config
        self.symbol = symbol
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._bid_history: Dict[float, Deque[float]] = defaultdict(
            lambda: deque(maxlen=config.history_size)
        )
        self._ask_history: Dict[float, Deque[float]] = defaultdict(
            lambda: deque(maxlen=config.history_size)
        )
        self._bid_first_seen: Dict[float, float] = {}
        self._ask_first_seen: Dict[float, float] = {}
        self._bid_last_update: Dict[float, float] = {}
        self._ask_last_update: Dict[float, float] = {}
        self._last_update_ts: float = 0.0
        self._update_count: int = 0
        self._lock = asyncio.Lock()
        logger.debug(f"OrderBookAnalyzer инициализирован: {symbol}, "
                     f"wall_thr={config.wall_threshold}, "
                     f"melt_thr={config.melt_speed_threshold}")

    async def update(self, depth_data: dict) -> Optional[AnalyzedOrderBook]:
        """Обновить состояние стакана и провести анализ.

        Обрабатывает WebSocket-сообщение типа spot@public.depth.v3.api.
        Поддерживает как полные снимки (snapshot), так и инкрементальные обновления.

        Args:
            depth_data: Словарь с данными стакана. Ожидаемые ключи:
                        - 'bids': list of [price_str, volume_str]
                        - 'asks': list of [price_str, volume_str]
                        Если volume == "0", уровень удаляется.

        Returns:
            AnalyzedOrderBook с результатами анализа, или None при ошибке.

        Raises:
            KeyError: Если структура данных неожиданная (логируется, не пробрасывается).
        """
        async with self._lock:
            try:
                now = time.time()
                bids_raw = depth_data.get("bids", [])
                asks_raw = depth_data.get("asks", [])

                self._apply_updates(bids_raw, is_bid=True, now=now)
                self._apply_updates(asks_raw, is_bid=False, now=now)

                self._last_update_ts = now
                self._update_count += 1

                return self._analyze(now)

            except Exception as exc:
                logger.error(f"Ошибка при обновлении стакана [{self.symbol}]: {exc}",
                             exc_info=True)
                return None

    def _apply_updates(
        self,
        levels_raw: list,
        is_bid: bool,
        now: float,
    ) -> None:
        """Применить обновление уровней к внутреннему состоянию стакана.

        Args:
            levels_raw: Список пар [price_str, volume_str] из WebSocket.
            is_bid:     True для bid, False для ask.
            now:        Текущий Unix-timestamp.
        """
        book = self._bids if is_bid else self._asks
        history = self._bid_history if is_bid else self._ask_history
        first_seen = self._bid_first_seen if is_bid else self._ask_first_seen
        last_update = self._bid_last_update if is_bid else self._ask_last_update

        for item in levels_raw:
            try:
                price = float(item[0])
                volume = float(item[1])
            except (IndexError, ValueError, TypeError) as exc:
                logger.warning(f"Некорректный уровень стакана {item}: {exc}")
                continue

            if volume == 0.0:
                # Уровень исчез — удалить из книги и истории
                book.pop(price, None)
                history.pop(price, None)
                first_seen.pop(price, None)
                last_update.pop(price, None)
            else:
                prev_volume = book.get(price, 0.0)
                book[price] = volume
                history[price].append(prev_volume if prev_volume > 0 else volume)
                if price not in first_seen:
                    first_seen[price] = now
                last_update[price] = now

    def _analyze(self, now: float) -> Optional[AnalyzedOrderBook]:
        """Провести полный анализ текущего состояния стакана.

        Вычисляет: best bid/ask, mid_price, spread, VWAP, дисбаланс,
        обнаруживает стены и тающие стены.

        Args:
            now: Текущий Unix-timestamp.

        Returns:
            AnalyzedOrderBook или None если стакан пуст.
        """
        if not self._bids or not self._asks:
            logger.debug(f"Стакан {self.symbol} пуст, анализ пропущен")
            return None

        # Сортировка: bids убывают, asks возрастают
        sorted_bids = sorted(self._bids.items(), reverse=True)
        sorted_asks = sorted(self._asks.items())

        best_bid = sorted_bids[0][0]
        best_ask = sorted_asks[0][0]

        if best_bid >= best_ask:
            logger.warning(f"Инвертированный спред [{self.symbol}]: "
                           f"bid={best_bid} >= ask={best_ask}, пропуск")
            return None

        mid_price = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid_price * 100.0

        if spread_pct > self.config.spread_max_pct:
            logger.debug(f"Спред {spread_pct:.4f}% > порога {self.config.spread_max_pct}%, "
                         f"анализ пропущен")

        # VWAP
        vwap_bid = self._calc_vwap(sorted_bids[:self.config.imbalance_levels])
        vwap_ask = self._calc_vwap(sorted_asks[:self.config.imbalance_levels])

        # Дисбаланс
        k = self.config.imbalance_levels
        bid_vols = [v for _, v in sorted_bids[:k]]
        ask_vols = [v for _, v in sorted_asks[:k]]
        total_bid = sum(bid_vols)
        total_ask = sum(ask_vols)
        imbalance = total_bid / total_ask if total_ask > 0 else 1.0

        # OrderLevel objects
        bid_levels = self._build_levels(sorted_bids, is_bid=True, now=now)
        ask_levels = self._build_levels(sorted_asks, is_bid=False, now=now)

        # Стены
        bid_avg = total_bid / len(bid_vols) if bid_vols else 0.0
        ask_avg = total_ask / len(ask_vols) if ask_vols else 0.0

        bid_walls = self._detect_walls(
            bid_levels, bid_avg, best_bid, "bid", now
        )
        ask_walls = self._detect_walls(
            ask_levels, ask_avg, best_ask, "ask", now
        )

        melting_bid = [w for w in bid_walls if w.is_melting]
        melting_ask = [w for w in ask_walls if w.is_melting]

        return AnalyzedOrderBook(
            symbol=self.symbol,
            timestamp=now,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread_pct=spread_pct,
            imbalance=imbalance,
            bid_walls=bid_walls,
            ask_walls=ask_walls,
            melting_bid_walls=melting_bid,
            melting_ask_walls=melting_ask,
            total_bid_volume=total_bid,
            total_ask_volume=total_ask,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            vwap_bid=vwap_bid,
            vwap_ask=vwap_ask,
        )

    def _build_levels(
        self,
        sorted_items: List[Tuple[float, float]],
        is_bid: bool,
        now: float,
    ) -> List[OrderLevel]:
        """Построить список OrderLevel из отсортированных пар (price, volume).

        Args:
            sorted_items: Отсортированные пары [(price, volume), ...].
            is_bid:        True для bid.
            now:           Текущий Unix-timestamp.

        Returns:
            Список OrderLevel с историей объёмов.
        """
        history = self._bid_history if is_bid else self._ask_history
        first_seen = self._bid_first_seen if is_bid else self._ask_first_seen
        last_update = self._bid_last_update if is_bid else self._ask_last_update

        levels = []
        for price, volume in sorted_items:
            hist = list(history[price])
            prev_vol = hist[-1] if hist else volume
            level = OrderLevel(
                price=price,
                volume=volume,
                prev_volume=prev_vol,
                first_seen_ts=first_seen.get(price, now),
                last_update_ts=last_update.get(price, now),
            )
            levels.append(level)
        return levels

    def _detect_walls(
        self,
        levels: List[OrderLevel],
        avg_volume: float,
        best_price: float,
        side: str,
        now: float,
    ) -> List[Wall]:
        """Обнаружить стены и определить тающие среди них.

        Стена = уровень с объёмом > avg_volume * wall_threshold.
        Глубина стены от лучшей цены должна быть в диапазоне
        [min_wall_depth%, max_wall_depth%].

        Args:
            levels:     Список уровней стакана.
            avg_volume: Средний объём уровней для данной стороны.
            best_price: Лучшая цена данной стороны.
            side:       'bid' или 'ask'.
            now:        Текущий Unix-timestamp.

        Returns:
            Список обнаруженных Wall объектов (включая тающие).
        """
        walls = []
        threshold = avg_volume * self.config.wall_threshold

        if threshold <= 0:
            return walls

        for level in levels:
            if level.volume < threshold:
                continue

            if best_price <= 0:
                continue

            depth_pct = abs(level.price - best_price) / best_price * 100.0

            if not (self.config.min_wall_depth <= depth_pct <= self.config.max_wall_depth):
                continue

            melt_rate = level.melt_rate
            is_melting = melt_rate >= self.config.melt_speed_threshold

            wall = Wall(
                side=side,
                price=level.price,
                volume=level.volume,
                initial_volume=level.prev_volume if level.prev_volume > 0 else level.volume,
                detected_at=level.first_seen_ts,
                is_melting=is_melting,
                melt_rate=melt_rate,
                depth_from_best=depth_pct,
            )
            walls.append(wall)

            if is_melting:
                logger.info(
                    f"[{self.symbol}] Тающая {side}-стена @ {level.price:.4f} "
                    f"vol={level.volume:.4f}, rate={melt_rate:.2f}%/s, depth={depth_pct:.3f}%"
                )

        return walls

    @staticmethod
    def _calc_vwap(levels: List[Tuple[float, float]]) -> float:
        """Рассчитать VWAP (Volume Weighted Average Price) по списку уровней.

        Args:
            levels: Список пар (price, volume).

        Returns:
            VWAP как float. 0.0 если суммарный объём нулевой.
        """
        total_volume = sum(v for _, v in levels)
        if total_volume <= 0:
            return 0.0
        return sum(p * v for p, v in levels) / total_volume

    def reset(self) -> None:
        """Сбросить все внутренние данные стакана.

        Используется при переподключении WebSocket или смене символа.
        """
        self._bids.clear()
        self._asks.clear()
        self._bid_history.clear()
        self._ask_history.clear()
        self._bid_first_seen.clear()
        self._ask_first_seen.clear()
        self._bid_last_update.clear()
        self._ask_last_update.clear()
        self._last_update_ts = 0.0
        self._update_count = 0
        logger.info(f"OrderBookAnalyzer [{self.symbol}] сброшен")

    @property
    def update_count(self) -> int:
        """Количество успешных обновлений стакана с момента старта/сброса."""
        return self._update_count

    @property
    def last_update_ts(self) -> float:
        """Unix-timestamp последнего успешного обновления стакана."""
        return self._last_update_ts

    @property
    def is_stale(self) -> bool:
        """True если стакан не обновлялся более 10 секунд."""
        return time.time() - self._last_update_ts > 10.0

    def get_book_snapshot(self) -> dict:
        """Получить текущий снимок стакана в виде словаря.

        Returns:
            dict с ключами 'bids', 'asks', 'timestamp', 'symbol'.
        """
        return {
            "symbol": self.symbol,
            "timestamp": self._last_update_ts,
            "bids": sorted(self._bids.items(), reverse=True)[:20],
            "asks": sorted(self._asks.items())[:20],
        }

    def __repr__(self) -> str:
        return (f"OrderBookAnalyzer({self.symbol}, "
                f"bids={len(self._bids)}, asks={len(self._asks)}, "
                f"updates={self._update_count})")
