"""
orchestrator.py — Главный оркестратор CONFI Scalper.

Координирует работу всех модулей бота:
    - Инициализация и запуск всех компонентов.
    - Главный цикл: анализ стакана → решение → проверка рисков → исполнение.
    - Буферизация последнего WebSocket-снапшота.
    - Обработка сигналов операционной системы (SIGINT, SIGTERM).
    - Graceful shutdown: закрытие позиций, сохранение статистики.
    - Периодические задачи: сохранение дневной статистики, сброс балансов.

Поток данных:
    MexcClient(WS) → on_depth → analyzer.update() → decision.evaluate()
                                                    ↓
                              risk_manager.can_trade() → executor.open_position()
                                                    ↓
                                    notifier → logger_stats (SQLite)

Использование:
    config = load_config("config.yaml")
    bot = Orchestrator(config)
    await bot.run()
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from models import (
    AnalyzedOrderBook,
    BotConfig,
    Direction,
    ExitReason,
)
from modules.analyzer import OrderBookAnalyzer
from modules.decision import DecisionEngine
from modules.executor import TradeExecutor
from modules.logger_stats import StatsLogger
from modules.mexc_client import MexcClient
from modules.notifier import Notifier
from modules.risk_manager import RiskManager

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


class Orchestrator:
    """Главный оркестратор бота. Точка входа для запуска торгового цикла.

    Запускает все модули, соединяет их callback-архитектурой и управляет
    жизненным циклом бота от старта до корректной остановки.

    Attributes:
        config:          Главная конфигурация.
        analyzer:        Анализатор стакана.
        decision:        Движок принятия решений.
        risk_manager:    Менеджер рисков.
        executor:        Исполнитель сделок.
        mexc_client:     Клиент биржи.
        notifier:        Модуль уведомлений.
        stats:           Модуль статистики и БД.
        _running:        Флаг активности главного цикла.
        _last_ob:        Последний проанализированный стакан.
        _ob_lock:        Lock для буфера стакана.
        _signal_count:   Счётчик сгенерированных сигналов за сессию.
        _loop_count:     Счётчик итераций главного цикла.
        _start_time:     Timestamp запуска бота.
    """

    # Интервал сохранения дневной статистики
    STATS_SAVE_INTERVAL = 3600.0   # 1 час
    # Интервал проверки смены дня
    DAY_CHECK_INTERVAL = 300.0     # 5 минут
    # Интервал логирования статуса
    STATUS_LOG_INTERVAL = 600.0    # 10 минут

    def __init__(self, config: BotConfig) -> None:
        """Инициализация оркестратора.

        Args:
            config: Главная конфигурация бота.
        """
        self.config = config

        # Инициализация всех модулей
        self.analyzer = OrderBookAnalyzer(config.analyzer, config.symbol)
        self.decision = DecisionEngine(config)
        self.risk_manager = RiskManager(config)
        self.notifier = Notifier(config.telegram)
        self.stats = StatsLogger(config.logging)
        self.mexc_client = MexcClient(config, config.symbol)
        self.executor = TradeExecutor(config, self.mexc_client, self.notifier)

        # Привязка коллбеков
        self.mexc_client.on_depth = self._on_ws_depth
        self.mexc_client.on_trade = self._on_ws_trade
        self.mexc_client.on_reconnect = self._on_ws_reconnect
        self.executor.on_position_closed = self._on_position_closed

        # Внутреннее состояние
        self._running = False
        self._last_ob: Optional[AnalyzedOrderBook] = None
        self._ob_lock = asyncio.Lock()
        self._signal_count = 0
        self._executed_signal_count = 0
        self._loop_count = 0
        self._start_time: float = 0.0
        self._current_balance: float = 0.0
        self._last_stats_save: float = 0.0
        self._last_day_check: float = 0.0
        self._last_status_log: float = 0.0

        logger.info(f"Orchestrator инициализирован: symbol={config.symbol}, v{VERSION}")

    async def run(self) -> None:
        """Запустить бота. Основная точка входа.

        Инициализирует все компоненты, регистрирует обработчики сигналов ОС,
        запускает WebSocket и входит в главный цикл.

        Не возвращается до получения SIGINT/SIGTERM или внутренней ошибки.
        """
        self._running = True
        self._start_time = time.time()

        # Инициализация БД
        await self.stats.initialize()
        logger.info("БД инициализирована")

        # Запуск notifier
        await self.notifier.start()

        # Регистрация сигналов ОС
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        logger.info(f"CONFI Scalper v{VERSION} запускается: {self.config.symbol}")

        try:
            # Получить начальный баланс
            await self._init_balance()

            # Подключить WebSocket
            await self.mexc_client.connect()

            # Уведомление о старте
            await self.notifier.on_bot_started(VERSION, self.config.symbol)

            # Запуск вспомогательных задач
            periodic_task = asyncio.create_task(
                self._periodic_tasks(), name="periodic_tasks"
            )

            # Главный цикл
            await self._main_loop()

        except asyncio.CancelledError:
            logger.info("Главный цикл отменён")
        except Exception as exc:
            logger.critical(f"Критическая ошибка оркестратора: {exc}", exc_info=True)
            await self.notifier.on_error(str(exc), critical=True)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Инициировать корректную остановку бота.

        Вызывается при SIGINT/SIGTERM или из теста.
        """
        logger.info("Остановка бота инициирована")
        self._running = False

    async def _main_loop(self) -> None:
        """Главный цикл обработки сигналов.

        На каждой итерации:
        1. Читает последний проанализированный стакан из буфера.
        2. Вызывает DecisionEngine.evaluate().
        3. Если сигнал — проверяет риски и открывает позицию.

        Итерации выполняются с интервалом config.loop_interval_seconds.
        """
        logger.info("Главный цикл запущен")
        while self._running:
            loop_start = time.time()
            self._loop_count += 1

            try:
                async with self._ob_lock:
                    ob = self._last_ob

                if ob is not None:
                    await self._process_order_book(ob)

            except Exception as exc:
                logger.error(f"Ошибка в главном цикле #{self._loop_count}: {exc}",
                             exc_info=True)

            # Выдержать интервал
            elapsed = time.time() - loop_start
            sleep_time = max(0, self.config.loop_interval_seconds - elapsed)
            await asyncio.sleep(sleep_time)

        logger.info(f"Главный цикл завершён после {self._loop_count} итераций")

    async def _process_order_book(self, ob: AnalyzedOrderBook) -> None:
        """Обработать проанализированный стакан: оценить и исполнить сигнал.

        Args:
            ob: Проанализированный стакан.
        """
        # Оценка через DecisionEngine
        signal = self.decision.evaluate(ob)

        if signal is None:
            return

        self._signal_count += 1

        # Сохранить сигнал в БД
        await self.stats.save_signal(signal)

        # Проверка возможности торговли
        can_trade, reason = await self.risk_manager.can_trade(
            self.executor.open_positions_count
        )

        if not can_trade:
            logger.debug(f"Сигнал проигнорирован: {reason}")
            if "лимит" in reason.lower() or "убыток" in reason.lower():
                await self.notifier.on_risk_limit(reason)
            return

        # Открытие позиции
        position = await self.executor.open_position(
            signal, self._current_balance
        )

        if position:
            self._executed_signal_count += 1
            logger.info(
                f"Позиция открыта по сигналу #{self._signal_count}: "
                f"{signal.direction.value} {signal.symbol}"
            )

    async def _periodic_tasks(self) -> None:
        """Периодические фоновые задачи.

        Выполняет:
        - Сохранение дневной статистики каждый час.
        - Проверку смены дня каждые 5 минут.
        - Логирование статуса бота каждые 10 минут.
        """
        while self._running:
            now = time.time()

            # Сохранение статистики
            if now - self._last_stats_save >= self.STATS_SAVE_INTERVAL:
                await self._save_daily_stats()
                self._last_stats_save = now

            # Логирование статуса
            if now - self._last_status_log >= self.STATUS_LOG_INTERVAL:
                self._log_status()
                self._last_status_log = now

            await asyncio.sleep(30)  # Проверка каждые 30 секунд

    async def _save_daily_stats(self) -> None:
        """Рассчитать и сохранить суточную статистику."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            daily = await self.stats.compute_daily_stats(
                date=today,
                starting_balance=self.risk_manager._daily_start_balance,
                ending_balance=self._current_balance,
                signals_generated=self._signal_count,
                signals_executed=self._executed_signal_count,
            )
            await self.stats.upsert_daily_stats(daily)
            logger.info(
                f"Дневная статистика сохранена: {today}, "
                f"trades={daily.total_trades}, pnl={daily.net_pnl:+.4f}"
            )
        except Exception as exc:
            logger.error(f"Ошибка сохранения дневной статистики: {exc}")

    def _log_status(self) -> None:
        """Записать текущий статус бота в лог."""
        uptime = time.time() - self._start_time
        rm_stats = self.risk_manager.get_stats()
        dec_stats = self.decision.get_stats()

        logger.info(
            f"[STATUS] Uptime={uptime/3600:.1f}ч | "
            f"Loops={self._loop_count} | "
            f"Signals={self._signal_count} | "
            f"Executed={self._executed_signal_count} | "
            f"OpenPos={self.executor.open_positions_count} | "
            f"ClosedTrades={self.executor.closed_trades_count} | "
            f"DailyPnL={rm_stats['daily_pnl']:+.4f} | "
            f"ConsecLosses={rm_stats['consecutive_losses']} | "
            f"WS_reconnects={self.mexc_client.reconnect_count}"
        )

    async def _init_balance(self) -> None:
        """Получить и установить текущий баланс аккаунта."""
        if self.config.dry_run or self.mexc_client.mexc.api_key == "":
            self._current_balance = 1000.0  # Тестовый баланс
            logger.info(f"DRY-RUN: начальный баланс = {self._current_balance} USDT")
        else:
            try:
                balances = await self.mexc_client.get_balance()
                self._current_balance = balances.get("USDT", 0.0)
                logger.info(f"Баланс получен: {self._current_balance:.4f} USDT")
            except Exception as exc:
                logger.error(f"Не удалось получить баланс: {exc}")
                self._current_balance = 0.0

        self.risk_manager.set_daily_start_balance(self._current_balance)

    async def _shutdown(self) -> None:
        """Выполнить graceful shutdown.

        Последовательность:
        1. Закрыть все открытые позиции (ExitReason.SHUTDOWN).
        2. Сохранить финальную дневную статистику.
        3. Отправить уведомление об остановке.
        4. Остановить notifier и mexc_client.
        5. Закрыть БД.
        """
        logger.info("Graceful shutdown начат")

        # Закрыть позиции
        await self.executor.close_all_positions(ExitReason.SHUTDOWN)

        # Сохранить статистику
        await self._save_daily_stats()

        # Уведомление
        uptime = time.time() - self._start_time
        await self.notifier.on_bot_stopped(
            f"Штатная остановка. Uptime: {uptime/3600:.1f}ч"
        )

        # Остановить клиенты
        await self.mexc_client.disconnect()
        await self.notifier.stop()
        await self.stats.close()

        logger.info(
            f"CONFI Scalper v{VERSION} остановлен. "
            f"Uptime: {(time.time() - self._start_time)/3600:.2f}ч | "
            f"Trades: {self.executor.closed_trades_count} | "
            f"PnL: {self.executor.total_realized_pnl:+.4f} USDT"
        )

    # ------------------------------------------------------------------
    # Коллбеки WebSocket
    # ------------------------------------------------------------------

    async def _on_ws_depth(self, data: dict) -> None:
        """Обработать обновление стакана из WebSocket.

        Обновляет OrderBookAnalyzer и сохраняет результат в буфер _last_ob.

        Args:
            data: Данные стакана от MEXC WS.
        """
        analyzed = await self.analyzer.update(data)
        if analyzed is not None:
            async with self._ob_lock:
                self._last_ob = analyzed
            # Обновить цену в executor
            self.executor.update_price(analyzed.mid_price)

    async def _on_ws_trade(self, data: dict) -> None:
        """Обработать сделку из WebSocket (тик).

        Используется для обновления текущей цены если стакан устарел.

        Args:
            data: Данные сделки от MEXC WS.
        """
        try:
            deals = data.get("deals", [data])
            for deal in deals:
                price = float(deal.get("p", 0))
                if price > 0:
                    self.executor.update_price(price)
                    break
        except Exception as exc:
            logger.debug(f"Ошибка обработки тика: {exc}")

    def _on_ws_reconnect(self) -> None:
        """Обработать переподключение WebSocket.

        Сбрасывает стакан анализатора для получения свежего снапшота.
        """
        logger.warning("WebSocket переподключён — сброс стакана анализатора")
        self.analyzer.reset()
        asyncio.create_task(
            self.mexc_client.get_order_book_rest(),
        )

    async def _on_position_closed(self, trade) -> None:
        """Обработать закрытие позиции.

        Обновляет риск-менеджер и сохраняет сделку в БД.

        Args:
            trade: Закрытая сделка.
        """
        # Обновить риск-менеджер
        await self.risk_manager.on_position_closed(
            trade.net_pnl,
            set_start_balance=self._current_balance,
        )

        # Сохранить в БД
        await self.stats.save_trade(trade)

        # Обновить баланс (приблизительно)
        self._current_balance += trade.net_pnl

    def __repr__(self) -> str:
        return (f"Orchestrator(symbol={self.config.symbol}, "
                f"running={self._running}, "
                f"uptime={time.time()-self._start_time:.0f}s)")
