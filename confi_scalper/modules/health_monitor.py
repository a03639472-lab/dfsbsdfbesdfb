"""
modules/health_monitor.py — Монитор состояния бота.

Отслеживает:
    - Время работы WebSocket (последнее сообщение, число переподключений).
    - Задержку обработки данных стакана.
    - Использование памяти.
    - Периодически логирует сводку состояния.
    - Генерирует события при деградации (долгое молчание WS, высокая задержка).

Пример использования::

    monitor = HealthMonitor(ws_silence_threshold_sec=60.0)
    monitor.on_ws_message_received()   # вызывать при каждом WS-сообщении
    monitor.on_depth_processed(lag=0.1)  # вызывать после обработки стакана

    # В оркестраторе:
    issues = monitor.check_health()
    if issues:
        for issue in issues:
            logger.warning(f"Health: {issue}")
"""

from __future__ import annotations

import time
import os
import asyncio
from collections import deque
from typing import List, Optional
from loguru import logger


class HealthMonitor:
    """
    Отслеживает здоровье системы и уведомляет о проблемах.

    Все методы синхронные. Не требует отдельного event loop.
    """

    def __init__(
        self,
        ws_silence_threshold_sec: float = 60.0,
        max_lag_sec: float = 5.0,
        lag_history_size: int = 100,
        report_interval_sec: float = 300.0,
    ):
        """
        Args:
            ws_silence_threshold_sec: Сколько секунд молчания WS считается проблемой.
            max_lag_sec:              Максимально допустимая задержка обработки стакана (с).
            lag_history_size:         Сколько значений задержки хранить для расчёта среднего.
            report_interval_sec:      Интервал периодических health-отчётов в лог.
        """
        self.ws_silence_threshold = ws_silence_threshold_sec
        self.max_lag_sec          = max_lag_sec
        self.report_interval_sec  = report_interval_sec

        # Временные метки
        self._start_time: float        = time.time()
        self._last_ws_message: float   = time.time()
        self._last_depth_update: float = time.time()
        self._last_report: float       = time.time()

        # Счётчики
        self._ws_messages_total: int   = 0
        self._ws_messages_per_min: int = 0
        self._ws_min_start: float      = time.time()
        self._depth_updates: int       = 0
        self._ws_reconnects: int       = 0
        self._errors_total: int        = 0

        # Статистика задержек
        self._lag_history: deque = deque(maxlen=lag_history_size)
        self._max_observed_lag: float = 0.0

        # Флаги
        self._ws_connected: bool  = False
        self._degraded: bool      = False

    # ─── Уведомления от других модулей ───────────────────────────────────────

    def on_ws_connected(self):
        """Вызывается при успешном подключении WebSocket."""
        self._ws_connected = True
        logger.debug("HealthMonitor: WS подключён")

    def on_ws_disconnected(self):
        """Вызывается при разрыве WebSocket."""
        self._ws_connected = False
        self._ws_reconnects += 1
        logger.debug(f"HealthMonitor: WS отключён (всего переподключений: {self._ws_reconnects})")

    def on_ws_message_received(self):
        """
        Вызывается при каждом входящем сообщении из WebSocket.
        Обновляет метку времени последнего сообщения.
        """
        now = self._last_ws_message = time.time()
        self._ws_messages_total += 1

        # Подсчёт сообщений в минуту
        if now - self._ws_min_start >= 60.0:
            self._ws_messages_per_min = 0
            self._ws_min_start = now
        self._ws_messages_per_min += 1

    def on_depth_processed(self, lag_sec: float = 0.0):
        """
        Вызывается после обработки обновления стакана.

        Args:
            lag_sec: Задержка между получением данных и их обработкой.
        """
        self._last_depth_update = time.time()
        self._depth_updates += 1
        if lag_sec > 0:
            self._lag_history.append(lag_sec)
            if lag_sec > self._max_observed_lag:
                self._max_observed_lag = lag_sec

    def on_error(self):
        """Вызывается при любой ошибке в системе."""
        self._errors_total += 1

    # ─── Проверка состояния ───────────────────────────────────────────────────

    def check_health(self) -> List[str]:
        """
        Выполняет проверки и возвращает список обнаруженных проблем.

        Returns:
            Список строк с описанием проблем. Пустой список = всё хорошо.
        """
        issues: List[str] = []
        now = time.time()

        # 1. Долгое молчание WebSocket
        ws_silence = now - self._last_ws_message
        if ws_silence > self.ws_silence_threshold:
            issues.append(
                f"WebSocket молчит {ws_silence:.0f}с "
                f"(порог {self.ws_silence_threshold:.0f}с)"
            )

        # 2. Высокая задержка обработки
        if self._lag_history:
            avg_lag = sum(self._lag_history) / len(self._lag_history)
            if avg_lag > self.max_lag_sec:
                issues.append(
                    f"Высокая средняя задержка обработки: {avg_lag:.2f}с "
                    f"(лимит {self.max_lag_sec}с)"
                )

        # 3. Проверка WS-соединения
        if not self._ws_connected:
            issues.append("WebSocket не подключён")

        # Обновляем флаг деградации
        self._degraded = len(issues) > 0

        # Периодический отчёт
        if now - self._last_report >= self.report_interval_sec:
            self._last_report = now
            self._log_report()

        return issues

    def get_stats(self) -> dict:
        """
        Возвращает полную статистику здоровья системы.

        Returns:
            Словарь со всеми метриками.
        """
        now = time.time()
        avg_lag = (
            sum(self._lag_history) / len(self._lag_history)
            if self._lag_history else 0.0
        )

        # Использование памяти (через /proc/self/status на Linux)
        mem_mb = self._get_memory_mb()

        return {
            "uptime_sec":          round(now - self._start_time, 1),
            "ws_connected":        self._ws_connected,
            "ws_reconnects":       self._ws_reconnects,
            "ws_silence_sec":      round(now - self._last_ws_message, 1),
            "ws_messages_total":   self._ws_messages_total,
            "ws_msgs_per_min":     self._ws_messages_per_min,
            "depth_updates":       self._depth_updates,
            "avg_lag_sec":         round(avg_lag, 4),
            "max_lag_sec":         round(self._max_observed_lag, 4),
            "errors_total":        self._errors_total,
            "degraded":            self._degraded,
            "memory_mb":           mem_mb,
        }

    def _log_report(self):
        """Логирует периодический health-отчёт."""
        stats = self.get_stats()
        logger.info(
            f"HealthMonitor: uptime={stats['uptime_sec']:.0f}с | "
            f"ws={stats['ws_connected']} | "
            f"reconnects={stats['ws_reconnects']} | "
            f"msgs={stats['ws_messages_total']} | "
            f"lag={stats['avg_lag_sec']:.3f}с | "
            f"mem={stats['memory_mb']:.1f} МБ"
        )

    @staticmethod
    def _get_memory_mb() -> float:
        """Возвращает использование памяти процессом в МБ (Linux/macOS)."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024, 1)
        except Exception:
            pass
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            return round(usage.ru_maxrss / 1024, 1)
        except Exception:
            return 0.0
