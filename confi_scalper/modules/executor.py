"""
executor.py — Модуль исполнения сделок (Trade Executor).

Отвечает за:
    - Открытие позиций через MARKET ордер.
    - Немедленное размещение стоп-лосса и тейк-профита после входа.
    - Мониторинг статуса SL/TP ордеров (фоновая задача).
    - Закрытие позиций по SL/TP, принудительно или при шатдауне.
    - Обновление трейлинг-стопа при движении цены.
    - Колбэки при закрытии позиции для обновления риск-менеджера.

Жизненный цикл позиции:
    1. open_position(signal, balance) → MARKET ордер → выставление SL/TP.
    2. Фоновая задача _monitor_position() проверяет статус каждые 2с.
    3. При FILLED SL или TP → close_position(exit_reason) → коллбек.
    4. Принудительное закрытие: cancel SL/TP → MARKET ордер в обратном направлении.

Режим dry_run:
    Если config.dry_run = True, реальные ордера не размещаются.
    Симулируется заполнение по текущей цене стакана.

Использование:
    executor = TradeExecutor(config, mexc_client, notifier)
    executor.on_position_closed = risk_manager.on_position_closed
    await executor.open_position(signal, balance=1000.0)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Callable, Coroutine, Dict, List, Optional, Any

from models import (
    BotConfig,
    ClosedTrade,
    Direction,
    ExitReason,
    OrderStatus,
    Position,
    Signal,
    SignalReason,
)

logger = logging.getLogger(__name__)

AsyncPositionCallback = Callable[[ClosedTrade], Coroutine[Any, Any, None]]


class TradeExecutor:
    """Исполнитель торговых операций.

    Управляет жизненным циклом позиций: открытие, мониторинг, закрытие.

    Attributes:
        config:              Главная конфигурация бота.
        mexc:                Клиент биржи MEXC.
        notifier:            Модуль Telegram-уведомлений.
        on_position_closed:  Коллбек при закрытии позиции.
        _positions:          Словарь открытых позиций {position_id: Position}.
        _monitor_tasks:      Фоновые задачи мониторинга {position_id: Task}.
        _lock:               asyncio.Lock для потокобезопасной работы с позициями.
        _current_price:      Последняя известная цена (обновляется из WS).
        _closed_trades:      История закрытых сделок текущей сессии.
    """

    MONITOR_INTERVAL = 2.0    # Интервал проверки SL/TP в секундах
    ORDER_FILL_TIMEOUT = 10.0 # Максимальное ожидание исполнения MARKET ордера

    def __init__(self, config: BotConfig, mexc_client=None, notifier=None) -> None:
        """Инициализация исполнителя.

        Args:
            config:      Главная конфигурация бота.
            mexc_client: Клиент биржи (MexcClient или None для dry_run).
            notifier:    Модуль уведомлений (Notifier или None).
        """
        self.config = config
        self.mexc = mexc_client
        self.notifier = notifier
        self.on_position_closed: Optional[AsyncPositionCallback] = None
        self._positions: Dict[str, Position] = {}
        self._monitor_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._current_price: float = 0.0
        self._closed_trades: List[ClosedTrade] = []
        logger.info(
            f"TradeExecutor инициализирован: "
            f"dry_run={config.dry_run}, symbol={config.symbol}"
        )

    async def open_position(
        self, signal: Signal, balance: float
    ) -> Optional[Position]:
        """Открыть торговую позицию по сигналу.

        Последовательность:
        1. Рассчитать объём позиции.
        2. Разместить MARKET ордер.
        3. Дождаться исполнения ордера.
        4. Разместить SL и TP ордера.
        5. Запустить задачу мониторинга.

        Args:
            signal:  Торговый сигнал для открытия.
            balance: Свободный баланс для расчёта объёма.

        Returns:
            Position если открытие успешно, None при ошибке.
        """
        async with self._lock:
            if len(self._positions) >= self.config.risk.max_open_positions:
                logger.warning(
                    f"Лимит позиций ({self.config.risk.max_open_positions}) достигнут, "
                    f"открытие отклонено"
                )
                return None

        # Рассчитать объём
        quantity = self._calc_quantity(signal, balance)
        if quantity <= 0:
            logger.error(f"Объём позиции <= 0 ({quantity}), открытие отменено")
            return None

        position_id = str(uuid.uuid4())
        side = "BUY" if signal.direction == Direction.LONG else "SELL"

        logger.info(
            f"Открытие позиции {position_id[:8]}: "
            f"{signal.direction.value} {self.config.symbol} "
            f"qty={quantity:.8f} @ ~{signal.entry_price:.4f}"
        )

        try:
            # Размещение MARKET ордера
            entry_price, entry_order_id = await self._place_market_order(
                side=side,
                quantity=quantity,
                expected_price=signal.entry_price,
            )

            # Создать объект позиции
            position = Position(
                id=position_id,
                symbol=self.config.symbol,
                direction=signal.direction,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                entry_order_id=entry_order_id,
                opened_at=time.time(),
                signal=signal,
                current_price=entry_price,
                max_favorable_price=entry_price,
            )

            # Разместить SL/TP ордера
            await self._place_sl_tp_orders(position)

            # Зарегистрировать позицию
            async with self._lock:
                self._positions[position_id] = position

            # Запустить мониторинг
            task = asyncio.create_task(
                self._monitor_position(position_id),
                name=f"monitor_{position_id[:8]}"
            )
            self._monitor_tasks[position_id] = task

            # Уведомление
            if self.notifier:
                await self.notifier.on_position_opened(position)

            logger.info(
                f"Позиция {position_id[:8]} открыта: "
                f"entry={entry_price:.4f}, "
                f"sl={position.stop_loss:.4f}, "
                f"tp={position.take_profit:.4f}"
            )
            return position

        except Exception as exc:
            logger.error(
                f"Ошибка открытия позиции {position_id[:8]}: {exc}",
                exc_info=True
            )
            if self.notifier:
                await self.notifier.on_error(
                    f"Ошибка открытия позиции: {exc}", critical=True
                )
            return None

    async def close_position(
        self,
        position_id: str,
        exit_reason: ExitReason,
        exit_price: Optional[float] = None,
    ) -> Optional[ClosedTrade]:
        """Принудительно закрыть позицию.

        Отменяет SL/TP ордера и размещает закрывающий MARKET ордер.

        Args:
            position_id:  ID позиции.
            exit_reason:  Причина закрытия.
            exit_price:   Цена закрытия (None = текущая рыночная).

        Returns:
            ClosedTrade при успехе, None при ошибке или отсутствии позиции.
        """
        async with self._lock:
            position = self._positions.get(position_id)
            if position is None:
                logger.warning(f"Позиция {position_id[:8]} не найдена для закрытия")
                return None

        logger.info(
            f"Закрытие позиции {position_id[:8]} по причине: {exit_reason.value}"
        )

        try:
            # Отменить SL/TP ордера
            await self._cancel_sl_tp_orders(position)

            # Закрывающий ордер
            close_side = "SELL" if position.direction == Direction.LONG else "BUY"
            actual_exit_price, _ = await self._place_market_order(
                side=close_side,
                quantity=position.quantity,
                expected_price=exit_price or position.current_price,
            )

            # Рассчитать PnL
            trade = self._build_closed_trade(position, actual_exit_price, exit_reason)

            # Удалить позицию и остановить мониторинг
            async with self._lock:
                self._positions.pop(position_id, None)
                self._closed_trades.append(trade)

            monitor_task = self._monitor_tasks.pop(position_id, None)
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()

            # Уведомление
            if self.notifier:
                await self.notifier.on_position_closed(trade)

            # Коллбек риск-менеджера
            if self.on_position_closed:
                await self.on_position_closed(trade)

            logger.info(
                f"Позиция {position_id[:8]} закрыта: "
                f"exit={actual_exit_price:.4f}, "
                f"pnl={trade.net_pnl:+.4f} USDT [{trade.pnl_pct:+.2f}%]"
            )
            return trade

        except Exception as exc:
            logger.error(
                f"Ошибка закрытия позиции {position_id[:8]}: {exc}",
                exc_info=True
            )
            if self.notifier:
                await self.notifier.on_error(
                    f"Ошибка закрытия позиции: {exc}", critical=True
                )
            return None

    async def close_all_positions(self, reason: ExitReason = ExitReason.SHUTDOWN) -> None:
        """Закрыть все открытые позиции.

        Используется при штатной остановке бота или срабатывании риск-лимита.

        Args:
            reason: Причина закрытия (по умолчанию SHUTDOWN).
        """
        async with self._lock:
            position_ids = list(self._positions.keys())

        if not position_ids:
            logger.info("Нет открытых позиций для закрытия")
            return

        logger.info(f"Закрытие {len(position_ids)} позиций по причине {reason.value}")
        tasks = [
            self.close_position(pid, reason)
            for pid in position_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for pid, result in zip(position_ids, results):
            if isinstance(result, Exception):
                logger.error(f"Ошибка при закрытии {pid[:8]}: {result}")

    def update_price(self, price: float) -> None:
        """Обновить текущую рыночную цену (вызывается из WS-потока).

        Также обновляет цену и трейлинг-стоп для всех открытых позиций.

        Args:
            price: Текущая рыночная цена.
        """
        if price <= 0:
            return
        self._current_price = price

        for position in self._positions.values():
            position.current_price = price

            # Обновить пиковую цену для трейлинг-стопа
            if position.direction == Direction.LONG:
                if price > position.max_favorable_price:
                    position.max_favorable_price = price
            else:
                if position.max_favorable_price == 0 or price < position.max_favorable_price:
                    position.max_favorable_price = price

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _monitor_position(self, position_id: str) -> None:
        """Фоновая задача мониторинга статуса SL/TP ордеров.

        Каждые MONITOR_INTERVAL секунд проверяет статус ордеров.
        При FILLED SL/TP → закрывает позицию.
        При превышении max_position_age_sec → принудительно закрывает.

        Args:
            position_id: ID отслеживаемой позиции.
        """
        logger.debug(f"Мониторинг позиции {position_id[:8]} запущен")

        try:
            while True:
                await asyncio.sleep(self.MONITOR_INTERVAL)

                async with self._lock:
                    position = self._positions.get(position_id)

                if position is None:
                    break  # Позиция уже закрыта

                # Проверка таймаута
                if position.age_seconds > self.config.risk.max_position_age_sec:
                    logger.warning(
                        f"Позиция {position_id[:8]} превысила максимальный возраст "
                        f"({position.age_seconds:.0f}с), принудительное закрытие"
                    )
                    await self.close_position(position_id, ExitReason.TIMEOUT)
                    break

                # Проверка SL/TP ордеров
                if not self.config.dry_run and self.mexc:
                    await self._check_sl_tp_status(position)

                # Обновление трейлинг-стопа
                if self.config.risk.trailing_stop_enabled and self._current_price > 0:
                    from modules.risk_manager import RiskManager
                    # Трейлинг без экземпляра RM — вычисляем напрямую
                    trail_pct = self.config.risk.trailing_stop_pct / 100.0
                    if position.direction == Direction.LONG:
                        new_sl = position.max_favorable_price * (1 - trail_pct)
                        if new_sl > position.stop_loss:
                            logger.debug(
                                f"Трейлинг: SL {position.stop_loss:.4f} → {new_sl:.4f}"
                            )
                            position.stop_loss = new_sl

        except asyncio.CancelledError:
            logger.debug(f"Мониторинг позиции {position_id[:8]} остановлен")

    async def _check_sl_tp_status(self, position: Position) -> None:
        """Проверить статус SL и TP ордеров через REST API.

        Если один из ордеров исполнен — закрыть позицию.

        Args:
            position: Отслеживаемая позиция.
        """
        try:
            for order_id, exit_reason in [
                (position.sl_order_id, ExitReason.STOP_LOSS),
                (position.tp_order_id, ExitReason.TAKE_PROFIT),
            ]:
                if not order_id:
                    continue
                order = await self.mexc.get_order(position.symbol, order_id)
                status = order.get("status", "")

                if status == OrderStatus.FILLED.value:
                    exit_price = float(order.get("price", position.current_price))
                    logger.info(
                        f"Ордер {exit_reason.value} исполнен для позиции "
                        f"{position.id[:8]} @ {exit_price:.4f}"
                    )
                    await self.close_position(position.id, exit_reason, exit_price)
                    return

        except Exception as exc:
            logger.warning(
                f"Ошибка проверки SL/TP для {position.id[:8]}: {exc}"
            )

    async def _place_market_order(
        self, side: str, quantity: float, expected_price: float
    ) -> tuple[float, str]:
        """Разместить MARKET ордер и дождаться исполнения.

        В режиме dry_run — симулирует заполнение по expected_price.

        Args:
            side:           'BUY' или 'SELL'.
            quantity:       Объём.
            expected_price: Ожидаемая цена (для dry_run и оценки проскальзывания).

        Returns:
            Кортеж (actual_price, order_id).
        """
        if self.config.dry_run or self.mexc is None:
            logger.info(f"[DRY-RUN] MARKET {side} {quantity:.8f} @ {expected_price:.4f}")
            return expected_price, f"dry_{uuid.uuid4().hex[:16]}"

        order = await self.mexc.place_order(
            symbol=self.config.symbol,
            side=side,
            order_type="MARKET",
            quantity=quantity,
        )

        # Ждём исполнения
        order_id = str(order.get("orderId", ""))
        start = time.time()
        while time.time() - start < self.ORDER_FILL_TIMEOUT:
            order_info = await self.mexc.get_order(self.config.symbol, order_id)
            if order_info.get("status") == OrderStatus.FILLED.value:
                fill_price = float(order_info.get("price", expected_price))
                return fill_price, order_id
            await asyncio.sleep(0.2)

        # Таймаут — использовать ожидаемую цену
        logger.warning(f"Таймаут ожидания исполнения ордера {order_id}")
        return expected_price, order_id

    async def _place_sl_tp_orders(self, position: Position) -> None:
        """Разместить ордера стоп-лосса и тейк-профита.

        Args:
            position: Только что открытая позиция.
        """
        if self.config.dry_run or self.mexc is None:
            position.sl_order_id = f"dry_sl_{uuid.uuid4().hex[:12]}"
            position.tp_order_id = f"dry_tp_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[DRY-RUN] SL @ {position.stop_loss:.4f}, "
                f"TP @ {position.take_profit:.4f}"
            )
            return

        close_side = "SELL" if position.direction == Direction.LONG else "BUY"

        # Stop-Loss
        try:
            sl_order = await self.mexc.place_order(
                symbol=position.symbol,
                side=close_side,
                order_type="STOP_LOSS_LIMIT",
                quantity=position.quantity,
                price=position.stop_loss,
                stop_price=position.stop_loss,
            )
            position.sl_order_id = str(sl_order.get("orderId", ""))
            logger.debug(f"SL ордер размещён: {position.sl_order_id}")
        except Exception as exc:
            logger.error(f"Ошибка размещения SL ордера: {exc}")

        # Take-Profit
        try:
            tp_order = await self.mexc.place_order(
                symbol=position.symbol,
                side=close_side,
                order_type="TAKE_PROFIT_LIMIT",
                quantity=position.quantity,
                price=position.take_profit,
                stop_price=position.take_profit,
            )
            position.tp_order_id = str(tp_order.get("orderId", ""))
            logger.debug(f"TP ордер размещён: {position.tp_order_id}")
        except Exception as exc:
            logger.error(f"Ошибка размещения TP ордера: {exc}")

    async def _cancel_sl_tp_orders(self, position: Position) -> None:
        """Отменить SL и TP ордера при принудительном закрытии позиции.

        Args:
            position: Позиция с ID ордеров SL/TP.
        """
        if self.config.dry_run or self.mexc is None:
            return

        for order_id, name in [
            (position.sl_order_id, "SL"),
            (position.tp_order_id, "TP"),
        ]:
            if order_id and not order_id.startswith("dry_"):
                try:
                    await self.mexc.cancel_order(position.symbol, order_id)
                    logger.debug(f"{name} ордер {order_id} отменён")
                except Exception as exc:
                    logger.warning(f"Не удалось отменить {name} ордер {order_id}: {exc}")

    def _calc_quantity(self, signal: Signal, balance: float) -> float:
        """Рассчитать объём позиции.

        Args:
            signal:  Торговый сигнал.
            balance: Свободный баланс USDT.

        Returns:
            Объём в базовой валюте.
        """
        if balance <= 0 or signal.entry_price <= 0:
            return 0.0
        risk_amount = balance * self.config.risk.risk_per_trade_percent / 100.0
        return round(risk_amount / signal.entry_price, 8)

    def _build_closed_trade(
        self,
        position: Position,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> ClosedTrade:
        """Построить объект ClosedTrade из закрытой позиции.

        Args:
            position:    Закрытая позиция.
            exit_price:  Фактическая цена закрытия.
            exit_reason: Причина закрытия.

        Returns:
            ClosedTrade готовый для записи в БД.
        """
        commission_rate = self.config.risk.commission_rate
        commission = (position.entry_price + exit_price) * position.quantity * commission_rate

        if position.direction == Direction.LONG:
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity

        net_pnl = pnl - commission
        notional = position.entry_price * position.quantity
        pnl_pct = pnl / notional * 100 if notional > 0 else 0.0

        return ClosedTrade(
            id=position.id,
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
            signal_reason=position.signal.reason if position.signal else SignalReason.COMPOSITE,
            confidence=position.signal.confidence if position.signal else 0.0,
            duration_sec=position.age_seconds,
            opened_at=position.opened_at,
            closed_at=time.time(),
            commission=commission,
            net_pnl=net_pnl,
            max_favorable_price=position.max_favorable_price,
        )

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------

    @property
    def open_positions(self) -> Dict[str, Position]:
        """Словарь текущих открытых позиций (копия)."""
        return dict(self._positions)

    @property
    def open_positions_count(self) -> int:
        """Количество открытых позиций."""
        return len(self._positions)

    @property
    def closed_trades_count(self) -> int:
        """Количество закрытых сделок за сессию."""
        return len(self._closed_trades)

    @property
    def total_realized_pnl(self) -> float:
        """Суммарный реализованный PnL за сессию."""
        return sum(t.net_pnl for t in self._closed_trades)

    def __repr__(self) -> str:
        return (f"TradeExecutor("
                f"open={self.open_positions_count}, "
                f"closed={self.closed_trades_count}, "
                f"pnl={self.total_realized_pnl:+.4f})")
