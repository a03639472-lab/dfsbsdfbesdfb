"""
risk_manager.py — Модуль управления рисками (Risk Manager).

Отвечает за:
    - Проверку возможности открытия новой позиции.
    - Расчёт безопасного размера позиции.
    - Учёт дневных убытков и серий потерь.
    - Автоматический сброс дневных счётчиков при смене UTC-дня.
    - Трейлинг-стоп (опционально).

Правила запрета торговли (can_trade = False):
    1. Открытых позиций >= max_open_positions.
    2. Убытков подряд >= max_consecutive_losses.
    3. Дневной убыток >= max_daily_loss_percent от начального баланса дня.
    4. Баланс аккаунта <= 0.

Расчёт размера позиции:
    quantity = (balance * risk_per_trade_percent / 100) / entry_price

Использование:
    rm = RiskManager(config)
    if await rm.can_trade(open_positions_count):
        size = rm.calculate_position_size(balance, entry_price)
        sl, tp = rm.calculate_sl_tp(direction, entry_price)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from models import BotConfig, Direction, RiskConfig

logger = logging.getLogger(__name__)


class RiskManager:
    """Менеджер управления рисками торгового бота.

    Отслеживает состояние рисков в реальном времени и блокирует торговлю
    при нарушении лимитов.

    Attributes:
        config:                 Главная конфигурация бота.
        risk:                   Конфигурация рисков (ссылка на config.risk).
        _consecutive_losses:    Текущее число убыточных сделок подряд.
        _daily_pnl:             Суммарный PnL за текущие UTC-сутки.
        _daily_start_balance:   Баланс в начале текущих UTC-суток.
        _current_day:           Текущая дата UTC (для автосброса).
        _open_positions_count:  Текущее число открытых позиций.
        _total_trades:          Всего сделок за сессию.
        _total_wins:            Выигрышных сделок за сессию.
        _total_losses:          Убыточных сделок за сессию.
        _lock:                  asyncio.Lock для потокобезопасности.
        _paused_until:          Unix-timestamp, до которого торговля на паузе.
    """

    def __init__(self, config: BotConfig) -> None:
        """Инициализация риск-менеджера.

        Args:
            config: Главная конфигурация бота.
        """
        self.config = config
        self.risk: RiskConfig = config.risk
        self._consecutive_losses: int = 0
        self._daily_pnl: float = 0.0
        self._daily_start_balance: float = 0.0
        self._current_day: str = self._today_utc()
        self._open_positions_count: int = 0
        self._total_trades: int = 0
        self._total_wins: int = 0
        self._total_losses: int = 0
        self._lock = asyncio.Lock()
        self._paused_until: float = 0.0

        logger.info(
            f"RiskManager инициализирован: "
            f"risk_per_trade={self.risk.risk_per_trade_percent}%, "
            f"max_daily_loss={self.risk.max_daily_loss_percent}%, "
            f"max_consec_losses={self.risk.max_consecutive_losses}, "
            f"max_positions={self.risk.max_open_positions}"
        )

    async def can_trade(self, current_open_positions: int = 0) -> Tuple[bool, str]:
        """Проверить, разрешена ли торговля в данный момент.

        Проверяет все лимиты и возвращает причину запрета если торговля
        недоступна.

        Args:
            current_open_positions: Актуальное число открытых позиций.

        Returns:
            Кортеж (allowed: bool, reason: str).
            reason == '' если торговля разрешена.
        """
        async with self._lock:
            self._check_day_reset()
            self._open_positions_count = current_open_positions

            # Пауза по времени
            if time.time() < self._paused_until:
                remaining = self._paused_until - time.time()
                return False, f"Принудительная пауза ещё {remaining:.0f}с"

            # Лимит открытых позиций
            if self._open_positions_count >= self.risk.max_open_positions:
                return False, (
                    f"Открытых позиций {self._open_positions_count} >= "
                    f"лимит {self.risk.max_open_positions}"
                )

            # Серия убытков
            if self._consecutive_losses >= self.risk.max_consecutive_losses:
                return False, (
                    f"Убытков подряд {self._consecutive_losses} >= "
                    f"лимит {self.risk.max_consecutive_losses}"
                )

            # Дневной лимит убытков
            if self._daily_start_balance > 0:
                daily_loss_pct = (-self._daily_pnl / self._daily_start_balance * 100.0
                                  if self._daily_pnl < 0 else 0.0)
                if daily_loss_pct >= self.risk.max_daily_loss_percent:
                    return False, (
                        f"Дневной убыток {daily_loss_pct:.2f}% >= "
                        f"лимит {self.risk.max_daily_loss_percent}%"
                    )

            return True, ""

    def calculate_position_size(
        self,
        balance: float,
        entry_price: float,
        min_quantity: float = 0.000001,
    ) -> float:
        """Рассчитать размер позиции по правилу фиксированного процента риска.

        Формула: quantity = (balance * risk_pct) / entry_price

        Args:
            balance:      Свободный баланс аккаунта (USDT).
            entry_price:  Предполагаемая цена входа.
            min_quantity: Минимально допустимое количество (для фильтрации копеечных).

        Returns:
            Количество базовой валюты для открытия позиции.
            0.0 если входные данные некорректны.
        """
        if balance <= 0 or entry_price <= 0:
            logger.warning(
                f"Некорректные входные данные для calculate_position_size: "
                f"balance={balance}, entry_price={entry_price}"
            )
            return 0.0

        risk_amount = balance * self.risk.risk_per_trade_percent / 100.0
        quantity = risk_amount / entry_price

        if quantity < min_quantity:
            logger.warning(
                f"Рассчитанный объём {quantity:.8f} < минимального {min_quantity}, "
                f"позиция не будет открыта"
            )
            return 0.0

        logger.debug(
            f"Размер позиции: balance={balance:.2f} USDT, "
            f"risk={risk_amount:.4f} USDT, qty={quantity:.8f}"
        )
        return round(quantity, 8)

    def calculate_sl_tp(
        self, direction: Direction, entry_price: float
    ) -> Tuple[float, float]:
        """Рассчитать уровни стоп-лосса и тейк-профита.

        Args:
            direction:    Направление позиции.
            entry_price:  Цена входа.

        Returns:
            Кортеж (stop_loss, take_profit).
        """
        sl_pct = self.risk.stop_loss_percent / 100.0
        tp_pct = self.risk.take_profit_percent / 100.0

        if direction == Direction.LONG:
            stop_loss = entry_price * (1.0 - sl_pct)
            take_profit = entry_price * (1.0 + tp_pct)
        else:
            stop_loss = entry_price * (1.0 + sl_pct)
            take_profit = entry_price * (1.0 - tp_pct)

        return round(stop_loss, 8), round(take_profit, 8)

    def update_trailing_stop(
        self, direction: Direction, current_price: float, current_stop: float
    ) -> float:
        """Обновить трейлинг-стоп на основе текущей цены.

        Трейлинг-стоп двигается только в направлении прибыли, никогда назад.

        Args:
            direction:     Направление позиции.
            current_price: Текущая рыночная цена.
            current_stop:  Текущий уровень стоп-лосса.

        Returns:
            Новый уровень стоп-лосса (может совпадать с current_stop
            если цена не достигла нового пика).
        """
        if not self.risk.trailing_stop_enabled:
            return current_stop

        trail_pct = self.risk.trailing_stop_pct / 100.0

        if direction == Direction.LONG:
            new_stop = current_price * (1.0 - trail_pct)
            return max(new_stop, current_stop)
        else:
            new_stop = current_price * (1.0 + trail_pct)
            return min(new_stop, current_stop)

    async def on_position_closed(
        self, pnl: float, set_start_balance: Optional[float] = None
    ) -> None:
        """Обновить внутренние счётчики после закрытия позиции.

        Вызывается executor.py сразу после закрытия позиции.

        Args:
            pnl:               Реализованный PnL (положительный = прибыль).
            set_start_balance: Передать баланс аккаунта для инициализации
                               daily_start_balance если он ещё не установлен.
        """
        async with self._lock:
            self._check_day_reset()

            self._daily_pnl += pnl
            self._total_trades += 1

            if pnl > 0:
                self._total_wins += 1
                self._consecutive_losses = 0  # сбрасываем серию
                logger.debug(f"Прибыльная сделка: +{pnl:.4f}. "
                             f"Серия убытков сброшена.")
            else:
                self._total_losses += 1
                self._consecutive_losses += 1
                logger.warning(
                    f"Убыточная сделка: {pnl:.4f}. "
                    f"Серия убытков: {self._consecutive_losses}/"
                    f"{self.risk.max_consecutive_losses}"
                )
                if self._consecutive_losses >= self.risk.max_consecutive_losses:
                    logger.error(
                        f"ЛИМИТ УБЫТКОВ ПОДРЯД ДОСТИГНУТ: "
                        f"{self._consecutive_losses}. Торговля приостановлена."
                    )

            if set_start_balance is not None and self._daily_start_balance <= 0:
                self._daily_start_balance = set_start_balance
                logger.info(f"Начальный баланс дня установлен: {set_start_balance:.2f} USDT")

    def set_daily_start_balance(self, balance: float) -> None:
        """Установить начальный баланс текущего дня.

        Вызывается при старте бота или при смене дня.

        Args:
            balance: Баланс аккаунта на начало торгового дня.
        """
        self._daily_start_balance = balance
        logger.info(f"Начальный баланс дня установлен: {balance:.2f} USDT")

    def pause_trading(self, seconds: float) -> None:
        """Установить принудительную паузу торговли.

        Args:
            seconds: Длительность паузы в секундах.
        """
        self._paused_until = time.time() + seconds
        logger.warning(f"Торговля поставлена на паузу на {seconds:.0f}с")

    def resume_trading(self) -> None:
        """Снять принудительную паузу торговли досрочно."""
        self._paused_until = 0.0
        logger.info("Пауза торговли снята")

    def reset_consecutive_losses(self) -> None:
        """Вручную сбросить счётчик серийных убытков (например, после ручного анализа)."""
        self._consecutive_losses = 0
        logger.info("Счётчик серийных убытков сброшен вручную")

    def _check_day_reset(self) -> None:
        """Проверить наступление нового UTC-дня и сбросить суточные счётчики.

        Вызывается в начале каждой публичной операции. Не требует
        отдельной планировки — работает «ленивым» способом.
        """
        today = self._today_utc()
        if today != self._current_day:
            logger.info(
                f"Смена UTC-дня: {self._current_day} → {today}. "
                f"Сброс дневных счётчиков. "
                f"Итоги вчерашнего дня: PnL={self._daily_pnl:+.4f} USDT"
            )
            self._daily_pnl = 0.0
            self._daily_start_balance = 0.0
            self._current_day = today
            # Серию убытков НЕ сбрасываем — она сохраняется между днями

    @staticmethod
    def _today_utc() -> str:
        """Получить текущую дату UTC в формате YYYY-MM-DD."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @property
    def daily_pnl(self) -> float:
        """Суммарный PnL текущего UTC-дня."""
        return self._daily_pnl

    @property
    def daily_loss_pct(self) -> float:
        """Дневной убыток в % от начального баланса (0 если нет убытка)."""
        if self._daily_start_balance <= 0 or self._daily_pnl >= 0:
            return 0.0
        return -self._daily_pnl / self._daily_start_balance * 100.0

    @property
    def consecutive_losses(self) -> int:
        """Текущее число убыточных сделок подряд."""
        return self._consecutive_losses

    @property
    def win_rate(self) -> float:
        """Win rate за всю сессию [0..100]."""
        if self._total_trades == 0:
            return 0.0
        return self._total_wins / self._total_trades * 100.0

    def get_stats(self) -> dict:
        """Получить статистику риск-менеджера в виде словаря.

        Returns:
            dict с ключами: daily_pnl, daily_loss_pct, consecutive_losses,
            total_trades, total_wins, total_losses, win_rate, paused.
        """
        return {
            "date": self._current_day,
            "daily_pnl": self._daily_pnl,
            "daily_loss_pct": self.daily_loss_pct,
            "consecutive_losses": self._consecutive_losses,
            "max_consecutive_losses": self.risk.max_consecutive_losses,
            "total_trades": self._total_trades,
            "total_wins": self._total_wins,
            "total_losses": self._total_losses,
            "win_rate": self.win_rate,
            "paused": time.time() < self._paused_until,
            "paused_until": self._paused_until,
        }

    def __repr__(self) -> str:
        return (f"RiskManager("
                f"consec_losses={self._consecutive_losses}/{self.risk.max_consecutive_losses}, "
                f"daily_pnl={self._daily_pnl:+.4f}, "
                f"daily_loss={self.daily_loss_pct:.2f}%)")
