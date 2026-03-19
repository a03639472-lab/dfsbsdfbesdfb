"""
logger_stats.py — Модуль логирования и статистики (Logger & Stats).

Отвечает за:
    - Настройку loguru с ротацией файлов, retention и опциональным JSON-форматом.
    - Инициализацию SQLite базы данных (trades, signals, daily_stats).
    - Сохранение закрытых сделок и сигналов.
    - Расчёт и сохранение суточной статистики.
    - Экспорт отчётов в CSV.
    - Запрос статистики для ежедневных отчётов.

Схема SQLite:
    trades:
        id TEXT PK, symbol TEXT, direction TEXT, entry_price REAL,
        exit_price REAL, quantity REAL, pnl REAL, pnl_pct REAL,
        net_pnl REAL, commission REAL, exit_reason TEXT,
        signal_reason TEXT, confidence REAL, duration_sec REAL,
        opened_at REAL, closed_at REAL, max_favorable_price REAL
    signals:
        id INTEGER PK AUTOINCREMENT, symbol TEXT, direction TEXT,
        reason TEXT, confidence REAL, entry_price REAL,
        stop_loss REAL, take_profit REAL, timestamp REAL
    daily_stats:
        date TEXT PK, total_trades INT, winning_trades INT, ...

Использование:
    db = StatsLogger(config)
    await db.initialize()
    await db.save_trade(closed_trade)
    stats = await db.get_daily_stats("2025-01-15")
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from models import BotConfig, ClosedTrade, DailyStats, LoggingConfig, Signal

logger = logging.getLogger(__name__)


def setup_logging(config: LoggingConfig) -> None:
    """Настроить систему логирования loguru.

    Добавляет:
        - Файловый handler с ротацией и retention.
        - Консольный handler (если log_to_console = True).
        - Кастомный уровень TRADE (no=25) для торговых событий.

    Args:
        config: Конфигурация логирования.
    """
    try:
        from loguru import logger as loguru_logger
        import sys

        # Удалить существующие handlers
        loguru_logger.remove()

        # Формат
        fmt = ("{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
               "{name}:{function}:{line} | {message}")

        # Создать директорию для логов
        os.makedirs(os.path.dirname(config.log_file), exist_ok=True)

        # Файловый handler
        loguru_logger.add(
            config.log_file,
            level=config.level,
            format=fmt if not config.json_logs else "{message}",
            rotation=config.rotation,
            retention=config.retention,
            encoding="utf-8",
            serialize=config.json_logs,
        )

        # Консольный handler
        if config.log_to_console:
            loguru_logger.add(
                sys.stdout,
                level=config.level,
                format=fmt,
                colorize=True,
            )

        # Кастомный уровень TRADE
        try:
            loguru_logger.level("TRADE", no=25, color="<cyan>", icon="💹")
        except TypeError:
            pass  # Уровень уже существует

        logger.info(
            f"Логирование настроено: level={config.level}, "
            f"file={config.log_file}, json={config.json_logs}"
        )

    except ImportError:
        # Fallback на stdlib logging
        logging.basicConfig(
            level=getattr(logging, config.level.upper(), logging.INFO),
            format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
        )
        if config.log_to_console:
            logging.getLogger().addHandler(logging.StreamHandler())
        logger.info("loguru не найден, используется stdlib logging")


class StatsLogger:
    """Менеджер статистики и персистентного хранилища данных.

    Использует SQLite для хранения данных о сделках, сигналах
    и ежедневной статистике.

    Attributes:
        config:   Конфигурация логирования.
        db_file:  Путь к файлу SQLite.
        _conn:    Синхронное подключение SQLite (для пула).
        _lock:    asyncio.Lock для потокобезопасных запросов к БД.
    """

    def __init__(self, config: LoggingConfig) -> None:
        """Инициализация StatsLogger.

        Args:
            config: Конфигурация логирования и хранения данных.
        """
        self.config = config
        self.db_file = config.db_file
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Инициализировать БД: создать директорию, открыть соединение, создать таблицы.

        Вызывается один раз при запуске бота.

        Raises:
            sqlite3.Error: При ошибке создания или подключения к БД.
        """
        os.makedirs(os.path.dirname(self.db_file) or ".", exist_ok=True)

        async with self._lock:
            self._conn = sqlite3.connect(self.db_file, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._create_tables()

        logger.info(f"БД инициализирована: {self.db_file}")

    def _create_tables(self) -> None:
        """Создать таблицы SQLite если они не существуют."""
        assert self._conn is not None
        cursor = self._conn.cursor()

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                pnl REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                net_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                signal_reason TEXT NOT NULL,
                confidence REAL NOT NULL,
                duration_sec REAL NOT NULL,
                opened_at REAL NOT NULL,
                closed_at REAL NOT NULL,
                max_favorable_price REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                reason TEXT NOT NULL,
                confidence REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                risk_reward REAL NOT NULL,
                timestamp REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_commission REAL DEFAULT 0,
                net_pnl REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                avg_win REAL DEFAULT 0,
                avg_loss REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                sharpe_ratio REAL DEFAULT 0,
                starting_balance REAL DEFAULT 0,
                ending_balance REAL DEFAULT 0,
                signals_generated INTEGER DEFAULT 0,
                signals_executed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                stopped_at REAL,
                symbol TEXT NOT NULL,
                version TEXT NOT NULL,
                total_trades INTEGER DEFAULT 0,
                net_pnl REAL DEFAULT 0
            );
        """)
        self._conn.commit()

    async def save_trade(self, trade: ClosedTrade) -> None:
        """Сохранить закрытую сделку в БД.

        Args:
            trade: Закрытая сделка.
        """
        async with self._lock:
            if self._conn is None:
                logger.warning("БД не инициализирована, сделка не сохранена")
                return

            try:
                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO trades (
                        id, symbol, direction, entry_price, exit_price,
                        quantity, pnl, pnl_pct, net_pnl, commission,
                        exit_reason, signal_reason, confidence, duration_sec,
                        opened_at, closed_at, max_favorable_price
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        trade.id, trade.symbol, trade.direction.value,
                        trade.entry_price, trade.exit_price, trade.quantity,
                        trade.pnl, trade.pnl_pct, trade.net_pnl, trade.commission,
                        trade.exit_reason.value, trade.signal_reason.value,
                        trade.confidence, trade.duration_sec,
                        trade.opened_at, trade.closed_at, trade.max_favorable_price,
                    ),
                )
                self._conn.commit()
                logger.debug(f"Сделка {trade.id[:8]} сохранена в БД")
            except sqlite3.Error as exc:
                logger.error(f"Ошибка сохранения сделки {trade.id[:8]}: {exc}")

    async def save_signal(self, signal: Signal) -> None:
        """Сохранить торговый сигнал в БД.

        Args:
            signal: Торговый сигнал.
        """
        async with self._lock:
            if self._conn is None:
                return
            try:
                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO signals (
                        symbol, direction, reason, confidence,
                        entry_price, stop_loss, take_profit, risk_reward, timestamp
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        signal.symbol, signal.direction.value, signal.reason.value,
                        signal.confidence, signal.entry_price,
                        signal.stop_loss, signal.take_profit,
                        signal.risk_reward_ratio, signal.timestamp,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"Ошибка сохранения сигнала: {exc}")

    async def upsert_daily_stats(self, stats: DailyStats) -> None:
        """Сохранить или обновить суточную статистику.

        Args:
            stats: Суточная статистика.
        """
        async with self._lock:
            if self._conn is None:
                return
            try:
                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO daily_stats (
                        date, total_trades, winning_trades, losing_trades,
                        total_pnl, total_commission, net_pnl, win_rate,
                        avg_win, avg_loss, profit_factor, max_drawdown,
                        sharpe_ratio, starting_balance, ending_balance,
                        signals_generated, signals_executed
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stats.date, stats.total_trades, stats.winning_trades,
                        stats.losing_trades, stats.total_pnl, stats.total_commission,
                        stats.net_pnl, stats.win_rate, stats.avg_win, stats.avg_loss,
                        stats.profit_factor, stats.max_drawdown, stats.sharpe_ratio,
                        stats.starting_balance, stats.ending_balance,
                        stats.signals_generated, stats.signals_executed,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"Ошибка сохранения daily_stats: {exc}")

    async def get_daily_stats(self, date: str) -> Optional[DailyStats]:
        """Получить суточную статистику из БД.

        Args:
            date: Дата в формате 'YYYY-MM-DD'.

        Returns:
            DailyStats или None если данных нет.
        """
        async with self._lock:
            if self._conn is None:
                return None
            try:
                cursor = self._conn.cursor()
                cursor.execute(
                    "SELECT * FROM daily_stats WHERE date = ?", (date,)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return DailyStats(**dict(row))
            except sqlite3.Error as exc:
                logger.error(f"Ошибка получения daily_stats для {date}: {exc}")
                return None

    async def compute_daily_stats(
        self,
        date: str,
        starting_balance: float = 0.0,
        ending_balance: float = 0.0,
        signals_generated: int = 0,
        signals_executed: int = 0,
    ) -> DailyStats:
        """Вычислить суточную статистику из закрытых сделок за день.

        Рассчитывает: win_rate, avg_win/loss, profit_factor, max_drawdown,
        sharpe_ratio и другие метрики.

        Args:
            date:              Дата 'YYYY-MM-DD'.
            starting_balance:  Начальный баланс дня.
            ending_balance:    Конечный баланс дня.
            signals_generated: Кол-во сгенерированных сигналов.
            signals_executed:  Кол-во исполненных сигналов.

        Returns:
            DailyStats со всеми рассчитанными полями.
        """
        async with self._lock:
            if self._conn is None:
                return DailyStats(date=date)

            cursor = self._conn.cursor()
            # Сделки за день (по closed_at в пределах суток UTC)
            cursor.execute(
                """
                SELECT net_pnl, pnl_pct FROM trades
                WHERE date(closed_at, 'unixepoch') = ?
                """,
                (date,),
            )
            rows = cursor.fetchall()

        if not rows:
            return DailyStats(
                date=date,
                starting_balance=starting_balance,
                ending_balance=ending_balance,
                signals_generated=signals_generated,
                signals_executed=signals_executed,
            )

        pnls = [r[0] for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total_pnl = sum(pnls)
        win_rate = len(wins) / len(pnls) * 100 if pnls else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Максимальная просадка
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for p in pnls:
            cumulative += p
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_drawdown = max(max_drawdown, drawdown)

        # Коэффициент Шарпа (упрощённый)
        import statistics
        sharpe = 0.0
        if len(pnls) > 1:
            try:
                mean_pnl = statistics.mean(pnls)
                stdev_pnl = statistics.stdev(pnls)
                sharpe = mean_pnl / stdev_pnl if stdev_pnl > 0 else 0.0
            except Exception:
                pass

        return DailyStats(
            date=date,
            total_trades=len(pnls),
            winning_trades=len(wins),
            losing_trades=len(losses),
            total_pnl=total_pnl,
            net_pnl=total_pnl,  # комиссии уже учтены в net_pnl
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe,
            starting_balance=starting_balance,
            ending_balance=ending_balance,
            signals_generated=signals_generated,
            signals_executed=signals_executed,
        )

    async def export_trades_csv(self, path: str, date: Optional[str] = None) -> int:
        """Экспортировать сделки в CSV файл.

        Args:
            path: Путь к CSV файлу.
            date: Если задан — экспортировать только сделки за этот день.

        Returns:
            Количество экспортированных строк.
        """
        async with self._lock:
            if self._conn is None:
                return 0
            cursor = self._conn.cursor()
            if date:
                cursor.execute(
                    "SELECT * FROM trades WHERE date(closed_at,'unixepoch')=?", (date,)
                )
            else:
                cursor.execute("SELECT * FROM trades ORDER BY closed_at")
            rows = cursor.fetchall()

        if not rows:
            return 0

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([description[0] for description in cursor.description])
            writer.writerows(rows)

        logger.info(f"Экспортировано {len(rows)} сделок → {path}")
        return len(rows)

    async def close(self) -> None:
        """Закрыть подключение к SQLite."""
        async with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
        logger.info("Подключение к БД закрыто")

    def __repr__(self) -> str:
        return f"StatsLogger(db={self.db_file}, connected={self._conn is not None})"
