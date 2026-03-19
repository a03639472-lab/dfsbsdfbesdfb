"""
notifier.py — Модуль Telegram-уведомлений (Notifier).

Отправляет уведомления о ключевых событиях бота:
    - Запуск / остановка бота.
    - Генерация нового торгового сигнала.
    - Открытие / закрытие позиции.
    - Превышение лимитов риска.
    - Критические ошибки.
    - Ежедневный торговый отчёт.

Реализация:
    - Асинхронные запросы через aiohttp.
    - Retry с экспоненциальной задержкой (до 3 попыток).
    - Очередь сообщений через asyncio.Queue для предотвращения перегрузки.
    - Telegram Markdown v2 форматирование.
    - Автоматическое обрезание сообщений до max_message_len.

Использование:
    notifier = Notifier(config)
    await notifier.on_bot_started("v1.0")
    await notifier.on_position_opened(position)
    await notifier.on_position_closed(trade)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from models import (
    BotConfig,
    ClosedTrade,
    DailyStats,
    Direction,
    ExitReason,
    Position,
    Signal,
    TelegramConfig,
)

logger = logging.getLogger(__name__)


class Notifier:
    """Отправитель Telegram-уведомлений о событиях торгового бота.

    Attributes:
        config:        Конфигурация Telegram.
        _base_url:     Базовый URL Telegram Bot API.
        _queue:        asyncio.Queue для асинхронной отправки сообщений.
        _worker_task:  Фоновая задача-воркер, опустошающая очередь.
        _sent_count:   Счётчик отправленных сообщений.
        _error_count:  Счётчик ошибок отправки.
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2.0  # секунды

    def __init__(self, config: TelegramConfig) -> None:
        """Инициализация модуля уведомлений.

        Args:
            config: Конфигурация Telegram бота.
        """
        self.config = config
        self._base_url = self.BASE_URL.format(token=config.bot_token)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._worker_task: Optional[asyncio.Task] = None
        self._sent_count = 0
        self._error_count = 0
        self._running = False

        if not config.enabled:
            logger.info("Telegram-уведомления отключены в конфиге")

    async def start(self) -> None:
        """Запустить фоновый воркер отправки сообщений.

        Должен вызываться при старте оркестратора.
        """
        if not self.config.enabled:
            return
        self._running = True
        self._worker_task = asyncio.create_task(
            self._send_worker(), name="notifier_worker"
        )
        logger.info("Notifier запущен, воркер активен")

    async def stop(self) -> None:
        """Остановить воркер и дождаться отправки оставшихся сообщений.

        Должен вызываться при штатной остановке бота.
        """
        self._running = False
        # Ждём опустошения очереди (до 10 секунд)
        try:
            await asyncio.wait_for(self._queue.join(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Notifier: не все сообщения были отправлены при остановке")
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Notifier остановлен")

    # ------------------------------------------------------------------
    # Публичные методы событий
    # ------------------------------------------------------------------

    async def on_bot_started(self, version: str, symbol: str) -> None:
        """Уведомить о запуске бота.

        Args:
            version: Версия бота.
            symbol:  Торговый символ.
        """
        msg = (
            f"🟢 *CONFI Scalper запущен*\n"
            f"Версия: `{version}`\n"
            f"Символ: `{symbol}`\n"
            f"Время: {self._ts()}"
        )
        await self._enqueue(msg)

    async def on_bot_stopped(self, reason: str) -> None:
        """Уведомить об остановке бота.

        Args:
            reason: Причина остановки.
        """
        msg = (
            f"🔴 *CONFI Scalper остановлен*\n"
            f"Причина: {reason}\n"
            f"Время: {self._ts()}"
        )
        await self._enqueue(msg)

    async def on_signal(self, signal: Signal) -> None:
        """Уведомить о новом торговом сигнале.

        Отправляется только если config.notify_signals = True.

        Args:
            signal: Сгенерированный торговый сигнал.
        """
        if not self.config.notify_signals:
            return

        emoji = "📈" if signal.direction == Direction.LONG else "📉"
        msg = (
            f"{emoji} *Сигнал: {signal.direction.value}* `{signal.symbol}`\n"
            f"Причина: {signal.reason.value}\n"
            f"Уверенность: {signal.confidence:.1f}%\n"
            f"Цена: `{signal.entry_price:.4f}`\n"
            f"SL: `{signal.stop_loss:.4f}` | TP: `{signal.take_profit:.4f}`\n"
            f"RR: {signal.risk_reward_ratio:.2f}"
        )
        await self._enqueue(msg)

    async def on_position_opened(self, position: Position) -> None:
        """Уведомить об открытии позиции.

        Args:
            position: Открытая позиция.
        """
        if not self.config.notify_trades:
            return

        emoji = "🟢" if position.direction == Direction.LONG else "🔴"
        msg = (
            f"{emoji} *Позиция открыта* `{position.symbol}`\n"
            f"Направление: {position.direction.value}\n"
            f"Цена входа: `{position.entry_price:.4f}`\n"
            f"Объём: {position.quantity:.6f}\n"
            f"Стоимость: `{position.notional_value:.2f}` USDT\n"
            f"SL: `{position.stop_loss:.4f}` | TP: `{position.take_profit:.4f}`\n"
            f"ID: {position.id[:8]}"
        )
        await self._enqueue(msg)

    async def on_position_closed(self, trade: ClosedTrade) -> None:
        """Уведомить о закрытии позиции.

        Args:
            trade: Информация о закрытой сделке.
        """
        if not self.config.notify_trades:
            return

        if trade.is_winner:
            emoji = "✅"
            pnl_str = f"+{trade.net_pnl:.4f} USDT"
        else:
            emoji = "❌"
            pnl_str = f"{trade.net_pnl:.4f} USDT"

        reason_emoji = {
            ExitReason.STOP_LOSS: "🛑",
            ExitReason.TAKE_PROFIT: "🎯",
            ExitReason.MANUAL: "✋",
            ExitReason.TIMEOUT: "⏱",
            ExitReason.SHUTDOWN: "⚙️",
        }.get(trade.exit_reason, "❓")

        msg = (
            f"{emoji} *Позиция закрыта* `{trade.symbol}`\n"
            f"PnL: *{pnl_str}* ({trade.pnl_pct:+.2f}%)\n"
            f"{reason_emoji} Причина: {trade.exit_reason.value}\n"
            f"Вход: `{trade.entry_price:.4f}` → Выход: `{trade.exit_price:.4f}`\n"
            f"Длительность: {int(trade.duration_sec // 60)}м {int(trade.duration_sec % 60)}с\n"
            f"Комиссия: {trade.commission:.4f} USDT"
        )
        await self._enqueue(msg)

    async def on_risk_limit(self, reason: str) -> None:
        """Уведомить о срабатывании лимита риска.

        Args:
            reason: Описание нарушенного лимита.
        """
        msg = (
            f"⚠️ *Лимит риска сработал*\n"
            f"{reason}\n"
            f"Время: {self._ts()}"
        )
        await self._enqueue(msg)

    async def on_error(self, error: str, critical: bool = False) -> None:
        """Уведомить об ошибке бота.

        Args:
            error:    Описание ошибки.
            critical: Если True — добавляет пометку КРИТИЧЕСКАЯ.
        """
        if not self.config.notify_errors:
            return

        prefix = "🚨 *КРИТИЧЕСКАЯ ОШИБКА*" if critical else "⚠️ *Ошибка*"
        msg = (
            f"{prefix}\n"
            f"```\n{error[:500]}\n```\n"
            f"Время: {self._ts()}"
        )
        await self._enqueue(msg)

    async def on_daily_report(self, stats: DailyStats) -> None:
        """Отправить ежедневный торговый отчёт.

        Args:
            stats: Суточная статистика торгов.
        """
        pnl_emoji = "📈" if stats.net_pnl >= 0 else "📉"
        msg = (
            f"{pnl_emoji} *Дневной отчёт* {stats.date}\n\n"
            f"Сделок: {stats.total_trades} "
            f"(✅{stats.winning_trades} / ❌{stats.losing_trades})\n"
            f"Win Rate: {stats.win_rate:.1f}%\n"
            f"Чистый PnL: *{stats.net_pnl:+.2f} USDT* ({stats.return_pct:+.2f}%)\n"
            f"Profit Factor: {stats.profit_factor:.2f}\n"
            f"Средний выигрыш: +{stats.avg_win:.4f}\n"
            f"Средний проигрыш: {stats.avg_loss:.4f}\n"
            f"Макс просадка: {stats.max_drawdown:.2f} USDT\n"
            f"Комиссии: {stats.total_commission:.4f} USDT\n"
            f"Сигналов: {stats.signals_generated} → исполнено {stats.signals_executed}"
        )
        await self._enqueue(msg)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _enqueue(self, message: str) -> None:
        """Добавить сообщение в очередь отправки.

        Если уведомления отключены — игнорирует.
        Если очередь переполнена — логирует предупреждение.

        Args:
            message: Текст сообщения (Telegram Markdown).
        """
        if not self.config.enabled:
            return

        # Обрезать сообщение если оно длиннее лимита
        if len(message) > self.config.max_message_len:
            message = message[:self.config.max_message_len - 3] + "..."

        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("Очередь уведомлений переполнена, сообщение пропущено")

    async def _send_worker(self) -> None:
        """Фоновый воркер: читает очередь и отправляет сообщения в Telegram.

        Работает пока self._running == True или в очереди есть сообщения.
        При ошибке отправки — ждёт экспоненциально нарастающее время и повторяет.
        """
        logger.debug("Notifier worker запущен")
        while self._running or not self._queue.empty():
            try:
                message = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
                await self._send_with_retry(message)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Notifier worker: неожиданная ошибка: {exc}", exc_info=True)
        logger.debug("Notifier worker завершён")

    async def _send_with_retry(self, message: str) -> bool:
        """Отправить сообщение с повтором при неудаче.

        Args:
            message: Текст сообщения.

        Returns:
            True если успешно отправлено, False если все попытки исчерпаны.
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                success = await self._do_send(message)
                if success:
                    self._sent_count += 1
                    return True
            except Exception as exc:
                logger.warning(f"Попытка {attempt}/{self.MAX_RETRIES} отправки "
                               f"Telegram-сообщения: {exc}")

            if attempt < self.MAX_RETRIES:
                delay = self.RETRY_BASE_DELAY ** attempt
                await asyncio.sleep(delay)

        self._error_count += 1
        logger.error(f"Не удалось отправить сообщение после {self.MAX_RETRIES} попыток")
        return False

    async def _do_send(self, message: str) -> bool:
        """Выполнить фактический HTTP-запрос к Telegram Bot API.

        Использует aiohttp если доступен, иначе логирует (dry-run).

        Args:
            message: Текст сообщения.

        Returns:
            True при успешном ответе (status 200, ok: true).
        """
        try:
            import aiohttp
        except ImportError:
            logger.debug(f"[DRY-RUN] Telegram: {message[:100]}...")
            return True

        payload = {
            "chat_id": self.config.chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._base_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("ok", False)
                else:
                    text = await resp.text()
                    logger.warning(f"Telegram API вернул {resp.status}: {text[:200]}")
                    return False

    @staticmethod
    def _ts() -> str:
        """Получить текущее время UTC в читаемом формате."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def sent_count(self) -> int:
        """Количество успешно отправленных сообщений."""
        return self._sent_count

    @property
    def error_count(self) -> int:
        """Количество ошибок отправки."""
        return self._error_count

    def __repr__(self) -> str:
        return (f"Notifier(enabled={self.config.enabled}, "
                f"sent={self._sent_count}, errors={self._error_count})")
