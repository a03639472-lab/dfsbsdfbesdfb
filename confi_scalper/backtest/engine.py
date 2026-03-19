"""
backtest/engine.py — Движок бэктестирования CONFI Scalper.

Воспроизводит исторические данные стакана и прогоняет через тот же
анализатор и модуль принятия решений, что и живой бот. Позволяет оценить
эффективность стратегии на исторических данных без риска реального капитала.

Архитектура бэктеста:
    BacktestEngine принимает серию снимков стакана (OrderBookSnapshot)
    и воспроизводит их последовательно, вызывая:
        1. OrderBookAnalyzer.update() — анализ каждого снимка.
        2. DecisionEngine.evaluate()  — генерация сигнала.
        3. BacktestTradeSimulator     — симуляция исполнения.
        4. BacktestRiskManager        — расчёт PnL и метрик.

Модель исполнения:
    - Вход — по mid_price снимка, в котором сгенерирован сигнал.
    - Проскальзывание — задаётся в базисных пунктах (slippage_bps).
    - SL/TP — проверяются на каждом следующем снимке.
    - Комиссия — задаётся в процентах (commission_pct).

Метрики результата:
    - Общий PnL, Win Rate, Profit Factor.
    - Максимальная просадка (Max Drawdown).
    - Среднее число баров до выхода.
    - Sharpe Ratio (если задан risk_free_rate).
    - Кривая эквити.

Пример использования::

    engine = BacktestEngine(
        config=config,
        slippage_bps=5,
        commission_pct=0.1,
    )
    snapshots = load_snapshots_from_csv("data/btcusdt_depth.csv")
    result = engine.run(snapshots)
    print(result.summary())
    result.plot_equity_curve()
"""

from __future__ import annotations

import time
import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from enum import Enum

from loguru import logger

from models import (
    AnalyzedOrderBook, Signal, Direction, ExitReason,
    OrderLevel, DailyStats,
)
from modules.analyzer import OrderBookAnalyzer
from modules.decision import DecisionEngine
from modules.risk_manager import RiskManager


# ═══════════════════════════════════════════════════════════════════════════════
# Структуры данных бэктеста
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderBookSnapshot:
    """
    Один исторический снимок стакана ордеров.

    Attributes:
        timestamp: Unix-время снимка.
        symbol:    Торговый символ.
        bids:      Уровни покупки [[price, vol], ...].
        asks:      Уровни продажи [[price, vol], ...].
        source:    Источник данных (для маркировки).
    """
    timestamp: float
    symbol: str
    bids: List
    asks: List
    source: str = ""


@dataclass
class BacktestTrade:
    """
    Одна сделка в бэктесте.

    Attributes:
        entry_time:     Время входа.
        exit_time:      Время выхода.
        direction:      Направление позиции.
        entry_price:    Цена входа (с учётом проскальзывания).
        exit_price:     Цена выхода.
        quantity:       Объём позиции.
        stop_loss:      Цена стоп-лосса.
        take_profit:    Цена тейк-профита.
        pnl_gross:      PnL без комиссий.
        pnl_net:        PnL с учётом комиссий.
        commission:     Уплаченная комиссия.
        exit_reason:    Причина выхода (SL/TP/TIMEOUT).
        signal_confidence: Уверенность сигнала.
        bars_held:      Число снимков до выхода.
        signal_reason:  Причина генерации сигнала.
    """
    entry_time: float
    exit_time: float
    direction: Direction
    entry_price: float
    exit_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    pnl_gross: float
    pnl_net: float
    commission: float
    exit_reason: ExitReason
    signal_confidence: int
    bars_held: int = 0
    signal_reason: str = ""

    @property
    def is_win(self) -> bool:
        """True, если сделка прибыльна."""
        return self.pnl_net > 0

    @property
    def pnl_pct(self) -> float:
        """PnL в процентах от цены входа."""
        if self.entry_price == 0:
            return 0.0
        return self.pnl_net / (self.entry_price * self.quantity) * 100


@dataclass
class BacktestResult:
    """
    Результат бэктеста.

    Attributes:
        symbol:           Торговый символ.
        start_time:       Начало периода.
        end_time:         Конец периода.
        initial_balance:  Начальный баланс.
        final_balance:    Конечный баланс.
        trades:           Список всех сделок.
        equity_curve:     Кривая эквити [(timestamp, balance), ...].
        snapshots_processed: Число обработанных снимков.
        signals_generated:   Число сигналов.
        slippage_bps:     Проскальзывание в базисных пунктах.
        commission_pct:   Комиссия в %.
    """
    symbol: str
    start_time: float
    end_time: float
    initial_balance: float
    final_balance: float
    trades: List[BacktestTrade]
    equity_curve: List[Tuple[float, float]]
    snapshots_processed: int = 0
    signals_generated: int = 0
    slippage_bps: float = 0.0
    commission_pct: float = 0.0

    @property
    def total_trades(self) -> int:
        """Общее число сделок."""
        return len(self.trades)

    @property
    def wins(self) -> int:
        """Число прибыльных сделок."""
        return sum(1 for t in self.trades if t.is_win)

    @property
    def losses(self) -> int:
        """Число убыточных сделок."""
        return self.total_trades - self.wins

    @property
    def win_rate(self) -> float:
        """Доля прибыльных сделок (%)."""
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def total_pnl(self) -> float:
        """Суммарный PnL (нетто)."""
        return sum(t.pnl_net for t in self.trades)

    @property
    def total_commission(self) -> float:
        """Суммарная комиссия."""
        return sum(t.commission for t in self.trades)

    @property
    def profit_factor(self) -> float:
        """
        Profit Factor = суммарная прибыль / суммарный убыток.
        Значение > 1 = стратегия прибыльна.
        """
        gross_profit = sum(t.pnl_net for t in self.trades if t.pnl_net > 0)
        gross_loss   = abs(sum(t.pnl_net for t in self.trades if t.pnl_net < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return round(gross_profit / gross_loss, 4)

    @property
    def max_drawdown(self) -> float:
        """
        Максимальная просадка в % от пика баланса.

        Returns:
            Значение просадки (0–100%). Отрицательное = просадка.
        """
        if not self.equity_curve:
            return 0.0
        peak  = self.equity_curve[0][1]
        max_dd = 0.0
        for _, balance in self.equity_curve:
            if balance > peak:
                peak = balance
            dd = (balance - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd
        return round(max_dd, 4)

    @property
    def avg_bars_held(self) -> float:
        """Среднее число баров в сделке."""
        if not self.trades:
            return 0.0
        return sum(t.bars_held for t in self.trades) / len(self.trades)

    @property
    def avg_win(self) -> float:
        """Средняя прибыль на прибыльной сделке."""
        wins = [t.pnl_net for t in self.trades if t.is_win]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        """Средний убыток на убыточной сделке."""
        losses = [t.pnl_net for t in self.trades if not t.is_win]
        return sum(losses) / len(losses) if losses else 0.0

    def sharpe_ratio(self, risk_free_rate: float = 0.0) -> float:
        """
        Коэффициент Шарпа на основе серии PnL.

        Args:
            risk_free_rate: Безрисковая ставка (% в год, аннуализированная).

        Returns:
            Sharpe Ratio.
        """
        returns = [t.pnl_net / self.initial_balance for t in self.trades]
        if len(returns) < 2:
            return 0.0
        n     = len(returns)
        mean  = sum(returns) / n
        var   = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std   = math.sqrt(var)
        if std == 0:
            return 0.0
        excess_return = mean - risk_free_rate / 365  # дневная безрисковая ставка
        return round(excess_return / std * math.sqrt(252), 4)  # аннуализация

    def exit_stats(self) -> dict:
        """
        Статистика по причинам выхода.

        Returns:
            Словарь: {причина_выхода: число_сделок}.
        """
        stats: Dict[str, int] = {}
        for t in self.trades:
            key = t.exit_reason.value
            stats[key] = stats.get(key, 0) + 1
        return stats

    def summary(self) -> str:
        """
        Возвращает текстовое резюме результатов бэктеста.

        Returns:
            Многострочная строка с ключевыми метриками.
        """
        period_days = (self.end_time - self.start_time) / 86400
        return (
            f"{'=' * 50}\n"
            f"  РЕЗУЛЬТАТЫ БЭКТЕСТА: {self.symbol}\n"
            f"{'=' * 50}\n"
            f"  Период:           {period_days:.1f} дней\n"
            f"  Снимков:          {self.snapshots_processed:,}\n"
            f"  Сигналов:         {self.signals_generated:,}\n"
            f"  Сделок всего:     {self.total_trades}\n"
            f"  Прибыльных:       {self.wins} ({self.win_rate:.1f}%)\n"
            f"  Убыточных:        {self.losses}\n"
            f"{'─' * 50}\n"
            f"  Начальный баланс: {self.initial_balance:.2f} USDT\n"
            f"  Конечный баланс:  {self.final_balance:.2f} USDT\n"
            f"  Суммарный PnL:    {self.total_pnl:+.4f} USDT\n"
            f"  Комиссии:         {self.total_commission:.4f} USDT\n"
            f"  Profit Factor:    {self.profit_factor:.3f}\n"
            f"  Max Drawdown:     {self.max_drawdown:.2f}%\n"
            f"  Sharpe Ratio:     {self.sharpe_ratio():.3f}\n"
            f"  Ср. прибыль:      {self.avg_win:.4f} USDT\n"
            f"  Ср. убыток:       {self.avg_loss:.4f} USDT\n"
            f"  Ср. баров:        {self.avg_bars_held:.1f}\n"
            f"{'─' * 50}\n"
            f"  Выходы:           {self.exit_stats()}\n"
            f"{'=' * 50}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Движок бэктеста
# ═══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Движок бэктестирования.

    Принимает снимки стакана и прогоняет их через те же модули,
    что используются в живом боте (analyzer + decision_engine).

    Не использует asyncio — весь бэктест синхронный, что упрощает
    параллельный запуск с разными параметрами.
    """

    def __init__(
        self,
        config: dict,
        slippage_bps: float = 5.0,
        commission_pct: float = 0.1,
        initial_balance: float = 1_000.0,
        max_position_bars: int = 300,
    ):
        """
        Args:
            config:            Конфигурация бота (тот же формат, что и для live).
            slippage_bps:      Проскальзывание в базисных пунктах (1 bps = 0.01%).
            commission_pct:    Комиссия за вход + выход в % от объёма.
            initial_balance:   Начальный баланс USDT для симуляции.
            max_position_bars: Максимальное число снимков в открытой позиции
                               (таймаут → принудительный выход по mid_price).
        """
        self.config          = config
        self.slippage_bps    = slippage_bps
        self.commission_pct  = commission_pct
        self.initial_balance = initial_balance
        self.max_bars        = max_position_bars

        risk_cfg = config["risk"]
        an_cfg   = config.get("analyzer", {})
        bot_cfg  = config["bot"]

        self.analyzer = OrderBookAnalyzer(
            wall_threshold=an_cfg.get("wall_threshold", 5.0),
            melt_speed_threshold=an_cfg.get("melt_speed_threshold", 10.0),
            history_size=an_cfg.get("history_size", 10),
        )
        self.decision_engine = DecisionEngine(
            confidence_threshold=bot_cfg.get("confidence_threshold", 70),
        )
        self.stop_loss_pct    = risk_cfg["stop_loss_percent"]
        self.take_profit_pct  = risk_cfg["take_profit_percent"]
        self.risk_pct         = risk_cfg.get("risk_per_trade_percent", 1.0)

    def run(self, snapshots: List[OrderBookSnapshot]) -> BacktestResult:
        """
        Запускает бэктест на серии снимков стакана.

        Args:
            snapshots: Список снимков в хронологическом порядке.

        Returns:
            BacktestResult с полной статистикой.
        """
        if not snapshots:
            raise ValueError("Список снимков пустой")

        logger.info(
            f"BacktestEngine: запуск бэктеста | "
            f"снимков={len(snapshots):,} | "
            f"баланс={self.initial_balance} USDT"
        )

        symbol      = snapshots[0].symbol
        balance     = self.initial_balance
        trades: List[BacktestTrade] = []
        equity: List[Tuple[float, float]] = [(snapshots[0].timestamp, balance)]

        signals_count = 0
        current_signal: Optional[Signal] = None
        signal_snapshot_idx: int         = 0

        for i, snap in enumerate(snapshots):
            # Анализ снимка
            try:
                analyzed = self.analyzer.update(snap.symbol, snap.bids, snap.asks)
            except Exception as exc:
                logger.debug(f"BacktestEngine: ошибка анализатора на снимке {i}: {exc!r}")
                continue

            # Проверяем SL/TP для открытой позиции
            if current_signal is not None:
                bars_in = i - signal_snapshot_idx
                trade, closed_balance = self._check_exit(
                    current_signal, analyzed, balance, bars_in
                )
                if trade is not None:
                    trades.append(trade)
                    balance = closed_balance
                    equity.append((snap.timestamp, balance))
                    current_signal = None
                    signal_snapshot_idx = 0
                    continue

                # Таймаут
                if bars_in >= self.max_bars:
                    trade = self._force_exit(current_signal, analyzed, balance, bars_in)
                    trades.append(trade)
                    balance = balance + trade.pnl_net
                    equity.append((snap.timestamp, balance))
                    current_signal = None
                    continue

            # Если нет открытой позиции — ищем сигнал
            if current_signal is None:
                sig = self.decision_engine.evaluate(
                    book=analyzed,
                    stop_loss_pct=self.stop_loss_pct,
                    take_profit_pct=self.take_profit_pct,
                )
                if sig:
                    signals_count += 1
                    current_signal      = sig
                    signal_snapshot_idx = i
                    # Применяем проскальзывание к цене входа
                    slip_factor = self.slippage_bps / 10_000
                    if sig.direction == Direction.LONG:
                        current_signal.entry_price *= (1 + slip_factor)
                    else:
                        current_signal.entry_price *= (1 - slip_factor)

        logger.info(
            f"BacktestEngine: завершён | "
            f"сигналов={signals_count} | сделок={len(trades)} | "
            f"баланс={balance:.2f} (старт={self.initial_balance:.2f})"
        )

        return BacktestResult(
            symbol=symbol,
            start_time=snapshots[0].timestamp,
            end_time=snapshots[-1].timestamp,
            initial_balance=self.initial_balance,
            final_balance=balance,
            trades=trades,
            equity_curve=equity,
            snapshots_processed=len(snapshots),
            signals_generated=signals_count,
            slippage_bps=self.slippage_bps,
            commission_pct=self.commission_pct,
        )

    def _check_exit(
        self,
        signal: Signal,
        analyzed: AnalyzedOrderBook,
        balance: float,
        bars_in: int,
    ) -> Tuple[Optional[BacktestTrade], float]:
        """
        Проверяет, сработал ли SL или TP на текущем снимке.

        Логика:
        - Для LONG: если mid_price <= stop_loss → SL. Если >= take_profit → TP.
        - Для SHORT: инвертировано.
        - SL имеет приоритет при одновременном срабатывании.

        Args:
            signal:   Активный сигнал (открытая позиция).
            analyzed: Текущий проанализированный стакан.
            balance:  Текущий баланс.
            bars_in:  Число снимков с момента входа.

        Returns:
            Кортеж (BacktestTrade или None, новый баланс).
        """
        mid = analyzed.mid_price
        if mid <= 0:
            return None, balance

        qty   = self._calc_qty(signal.entry_price, balance)
        is_sl = False
        is_tp = False

        if signal.direction == Direction.LONG:
            is_sl = mid <= signal.stop_loss
            is_tp = mid >= signal.take_profit
        else:
            is_sl = mid >= signal.stop_loss
            is_tp = mid <= signal.take_profit

        if not is_sl and not is_tp:
            return None, balance

        exit_reason = ExitReason.STOP_LOSS if is_sl else ExitReason.TAKE_PROFIT
        exit_price  = signal.stop_loss if is_sl else signal.take_profit

        trade = self._make_trade(signal, exit_price, qty, bars_in, exit_reason)
        new_balance = balance + trade.pnl_net
        return trade, new_balance

    def _force_exit(
        self,
        signal: Signal,
        analyzed: AnalyzedOrderBook,
        balance: float,
        bars_in: int,
    ) -> BacktestTrade:
        """
        Принудительно закрывает позицию по таймауту.

        Args:
            signal:   Активный сигнал.
            analyzed: Текущий стакан (цена выхода = mid_price).
            balance:  Текущий баланс.
            bars_in:  Число снимков в сделке.

        Returns:
            BacktestTrade с причиной TIMEOUT.
        """
        exit_price = analyzed.mid_price if analyzed.mid_price > 0 else signal.entry_price
        qty        = self._calc_qty(signal.entry_price, balance)
        return self._make_trade(signal, exit_price, qty, bars_in, ExitReason.TIMEOUT)

    def _make_trade(
        self,
        signal: Signal,
        exit_price: float,
        qty: float,
        bars_held: int,
        reason: ExitReason,
    ) -> BacktestTrade:
        """
        Создаёт объект BacktestTrade с расчётом PnL и комиссий.

        Args:
            signal:    Торговый сигнал.
            exit_price: Цена выхода.
            qty:        Объём позиции.
            bars_held:  Число снимков в сделке.
            reason:     Причина выхода.

        Returns:
            Полностью заполненный BacktestTrade.
        """
        if signal.direction == Direction.LONG:
            pnl_gross = (exit_price - signal.entry_price) * qty
        else:
            pnl_gross = (signal.entry_price - exit_price) * qty

        commission = (signal.entry_price + exit_price) * qty * self.commission_pct / 100
        pnl_net    = pnl_gross - commission

        return BacktestTrade(
            entry_time=signal.timestamp,
            exit_time=signal.timestamp + bars_held,
            direction=signal.direction,
            entry_price=signal.entry_price,
            exit_price=exit_price,
            quantity=qty,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            pnl_gross=round(pnl_gross, 8),
            pnl_net=round(pnl_net, 8),
            commission=round(commission, 8),
            exit_reason=reason,
            signal_confidence=signal.confidence,
            bars_held=bars_held,
            signal_reason=signal.reason.value,
        )

    def _calc_qty(self, entry_price: float, balance: float) -> float:
        """
        Рассчитывает объём позиции исходя из процента риска.

        Args:
            entry_price: Цена входа.
            balance:     Текущий баланс.

        Returns:
            Объём в базовой валюте.
        """
        usdt_size = balance * self.risk_pct / 100
        if entry_price <= 0:
            return 0.0
        return usdt_size / entry_price

    def parameter_sweep(
        self,
        snapshots: List[OrderBookSnapshot],
        wall_thresholds: List[float] = None,
        confidence_thresholds: List[int] = None,
    ) -> List[dict]:
        """
        Перебирает комбинации параметров и возвращает результаты.

        Позволяет найти оптимальные значения wall_threshold и
        confidence_threshold для заданного набора данных.

        Args:
            snapshots:             Снимки стакана.
            wall_thresholds:       Список значений wall_threshold.
            confidence_thresholds: Список значений confidence_threshold.

        Returns:
            Список словарей с параметрами и ключевыми метриками.
        """
        if wall_thresholds is None:
            wall_thresholds = [3.0, 4.0, 5.0, 6.0, 7.0]
        if confidence_thresholds is None:
            confidence_thresholds = [60, 65, 70, 75, 80]

        results = []
        total   = len(wall_thresholds) * len(confidence_thresholds)
        done    = 0

        logger.info(
            f"BacktestEngine: перебор параметров | "
            f"всего комбинаций={total}"
        )

        for wt in wall_thresholds:
            for ct in confidence_thresholds:
                # Обновляем параметры анализатора и движка решений
                self.analyzer = OrderBookAnalyzer(
                    wall_threshold=wt,
                    melt_speed_threshold=self.config["analyzer"]["melt_speed_threshold"],
                    history_size=self.config["analyzer"]["history_size"],
                )
                self.decision_engine = DecisionEngine(confidence_threshold=ct)
                self.analyzer.reset()

                result = self.run(snapshots)
                results.append({
                    "wall_threshold":       wt,
                    "confidence_threshold": ct,
                    "total_trades":         result.total_trades,
                    "win_rate":             result.win_rate,
                    "profit_factor":        result.profit_factor,
                    "total_pnl":            result.total_pnl,
                    "max_drawdown":         result.max_drawdown,
                    "sharpe_ratio":         result.sharpe_ratio(),
                })
                done += 1
                if done % 5 == 0:
                    logger.info(f"BacktestEngine: {done}/{total} комбинаций готово")

        # Сортируем по Profit Factor
        results.sort(key=lambda r: r["profit_factor"], reverse=True)
        return results


# ═══════════════════════════════════════════════════════════════════════════════
# Загрузчики данных
# ═══════════════════════════════════════════════════════════════════════════════

def load_snapshots_from_csv(path: str, symbol: str = "BTCUSDT") -> List[OrderBookSnapshot]:
    """
    Загружает снимки стакана из CSV-файла.

    Ожидаемый формат CSV:
        timestamp,bid_prices,bid_vols,ask_prices,ask_vols

    Где bid_prices/bid_vols/ask_prices/ask_vols — значения через '|'.

    Args:
        path:   Путь к CSV-файлу.
        symbol: Торговый символ.

    Returns:
        Список OrderBookSnapshot в хронологическом порядке.
    """
    import csv
    snapshots = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bid_prices = [float(p) for p in row["bid_prices"].split("|")]
                bid_vols   = [float(v) for v in row["bid_vols"].split("|")]
                ask_prices = [float(p) for p in row["ask_prices"].split("|")]
                ask_vols   = [float(v) for v in row["ask_vols"].split("|")]

                bids = [[p, v] for p, v in zip(bid_prices, bid_vols)]
                asks = [[p, v] for p, v in zip(ask_prices, ask_vols)]

                snapshots.append(OrderBookSnapshot(
                    timestamp=float(row["timestamp"]),
                    symbol=symbol,
                    bids=bids,
                    asks=asks,
                    source=path,
                ))
            except (KeyError, ValueError) as exc:
                logger.debug(f"Пропуск строки CSV: {exc}")

    logger.info(f"Загружено {len(snapshots):,} снимков из {path}")
    return snapshots


def generate_synthetic_snapshots(
    symbol: str = "BTCUSDT",
    base_price: float = 68_000.0,
    n_snapshots: int = 10_000,
    depth_levels: int = 20,
    seed: int = 42,
) -> List[OrderBookSnapshot]:
    """
    Генерирует синтетические снимки стакана для тестирования бэктеста.

    Моделирует:
    - Случайное блуждание цены.
    - Периодическое появление «стен» (крупных уровней).
    - Дисбаланс в случайные моменты.

    Args:
        symbol:      Торговый символ.
        base_price:  Начальная цена.
        n_snapshots: Число снимков.
        depth_levels: Глубина стакана.
        seed:        Инициализатор генератора случайных чисел.

    Returns:
        Список OrderBookSnapshot.
    """
    import random
    rng   = random.Random(seed)
    snaps = []
    price = base_price
    ts    = time.time()

    for i in range(n_snapshots):
        # Случайное блуждание цены (±0.05%)
        price *= (1 + rng.gauss(0, 0.0005))
        price  = max(price, 1.0)

        spread = price * 0.0002  # 0.02% спред

        bids = []
        asks = []

        for j in range(depth_levels):
            bid_price = price - spread / 2 - j * spread * 0.5
            ask_price = price + spread / 2 + j * spread * 0.5

            base_vol  = rng.uniform(0.1, 2.0)

            # Иногда добавляем «стену» (крупный объём)
            wall_factor = 1.0
            if rng.random() < 0.02 and j > 0:  # 2% вероятность
                wall_factor = rng.uniform(8.0, 20.0)

            bids.append([round(bid_price, 4), round(base_vol * wall_factor, 4)])
            asks.append([round(ask_price, 4), round(base_vol * wall_factor, 4)])

        snaps.append(OrderBookSnapshot(
            timestamp=ts + i * 1.0,
            symbol=symbol,
            bids=bids,
            asks=asks,
            source="synthetic",
        ))

    return snaps
