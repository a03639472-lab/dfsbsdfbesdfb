"""
models.py — Центральное хранилище всех dataclass-моделей проекта CONFI Scalper.

Все типы данных используемые во всех модулях: Direction, ExitReason, SignalReason,
OrderStatus, OrderLevel, Wall, AnalyzedOrderBook, Signal, Position, ClosedTrade,
DailyStats, Tick, Candle, BotConfig и все суб-конфиги.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any


class Direction(str, Enum):
    """Направление торговой позиции (LONG / SHORT / NONE)."""
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class ExitReason(str, Enum):
    """Причина закрытия позиции."""
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"
    RISK_LIMIT = "RISK_LIMIT"
    TIMEOUT = "TIMEOUT"
    REVERSAL = "REVERSAL"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"


class OrderStatus(str, Enum):
    """Статус биржевого ордера."""
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class SignalReason(str, Enum):
    """Причина генерации торгового сигнала."""
    MELTING_ASK_WALL = "melting_ask_wall"
    MELTING_BID_WALL = "melting_bid_wall"
    BID_IMBALANCE = "bid_imbalance"
    ASK_IMBALANCE = "ask_imbalance"
    STRONG_BID_WALL = "strong_bid_wall"
    STRONG_ASK_WALL = "strong_ask_wall"
    EMA_CROSS = "ema_cross"
    RSI_OVERSOLD = "rsi_oversold"
    RSI_OVERBOUGHT = "rsi_overbought"
    VWAP_BOUNCE = "vwap_bounce"
    COMPOSITE = "composite"


@dataclass
class OrderLevel:
    """Один ценовой уровень в стакане ордеров.

    Attributes:
        price:           Цена уровня.
        volume:          Суммарный объём на этом уровне (базовая валюта).
        prev_volume:     Объём на предыдущем обновлении (для расчёта скорости таяния).
        first_seen_ts:   Unix-timestamp первого появления уровня.
        last_update_ts:  Unix-timestamp последнего обновления объёма.
    """
    price: float
    volume: float
    prev_volume: float = 0.0
    first_seen_ts: float = field(default_factory=time.time)
    last_update_ts: float = field(default_factory=time.time)

    @property
    def melt_rate(self) -> float:
        """Скорость уменьшения объёма в % в секунду. 0 если уровень растёт."""
        elapsed = self.last_update_ts - self.first_seen_ts
        if elapsed <= 0 or self.prev_volume <= 0:
            return 0.0
        delta_pct = (self.prev_volume - self.volume) / self.prev_volume * 100.0
        return max(0.0, delta_pct / elapsed)

    def __repr__(self) -> str:
        return f"OrderLevel(price={self.price:.6f}, vol={self.volume:.4f}, melt={self.melt_rate:.2f}%/s)"


@dataclass
class Wall:
    """Значимая стена (крупный ордер) в стакане.

    Стена — уровень, объём которого превышает средний на wall_threshold крат.

    Attributes:
        side:             'bid' (поддержка) или 'ask' (сопротивление).
        price:            Цена стены.
        volume:           Текущий объём стены.
        initial_volume:   Объём при первом обнаружении.
        detected_at:      Timestamp обнаружения.
        is_melting:       True если активно уменьшается.
        melt_rate:        Скорость таяния в %/с.
        depth_from_best:  Расстояние от лучшей цены в %.
    """
    side: str
    price: float
    volume: float
    initial_volume: float = 0.0
    detected_at: float = field(default_factory=time.time)
    is_melting: bool = False
    melt_rate: float = 0.0
    depth_from_best: float = 0.0

    @property
    def volume_remaining_pct(self) -> float:
        """Процент оставшегося объёма относительно начального."""
        if self.initial_volume <= 0:
            return 100.0
        return self.volume / self.initial_volume * 100.0

    def __repr__(self) -> str:
        return (f"Wall({self.side} @ {self.price:.6f}, "
                f"vol={self.volume:.4f}/{self.initial_volume:.4f}, "
                f"melt={self.is_melting}, rate={self.melt_rate:.2f}%/s)")


@dataclass
class AnalyzedOrderBook:
    """Результат анализа стакана от analyzer.py, входной объект для decision.py.

    Attributes:
        symbol:            Торговый символ.
        timestamp:         Unix-timestamp анализа.
        best_bid:          Лучшая цена покупки.
        best_ask:          Лучшая цена продажи.
        mid_price:         Средняя цена (bid+ask)/2.
        spread_pct:        Спред в % от mid_price.
        imbalance:         bid_vol / ask_vol для верхних K уровней.
        bid_walls:         Стены покупателей.
        ask_walls:         Стены продавцов.
        melting_bid_walls: Тающие стены покупателей.
        melting_ask_walls: Тающие стены продавцов.
        total_bid_volume:  Суммарный объём bid (K уровней).
        total_ask_volume:  Суммарный объём ask (K уровней).
        bid_levels:        Все bid-уровни (убывание цены).
        ask_levels:        Все ask-уровни (возрастание цены).
        vwap_bid:          VWAP bid-стороны.
        vwap_ask:          VWAP ask-стороны.
    """
    symbol: str
    timestamp: float
    best_bid: float
    best_ask: float
    mid_price: float
    spread_pct: float
    imbalance: float
    bid_walls: List[Wall]
    ask_walls: List[Wall]
    melting_bid_walls: List[Wall]
    melting_ask_walls: List[Wall]
    total_bid_volume: float
    total_ask_volume: float
    bid_levels: List[OrderLevel] = field(default_factory=list)
    ask_levels: List[OrderLevel] = field(default_factory=list)
    vwap_bid: float = 0.0
    vwap_ask: float = 0.0

    @property
    def has_bid_wall(self) -> bool:
        """Есть ли хотя бы одна стена покупателей."""
        return len(self.bid_walls) > 0

    @property
    def has_ask_wall(self) -> bool:
        """Есть ли хотя бы одна стена продавцов."""
        return len(self.ask_walls) > 0

    @property
    def has_melting_bid_wall(self) -> bool:
        """Есть ли хотя бы одна тающая стена покупателей."""
        return len(self.melting_bid_walls) > 0

    @property
    def has_melting_ask_wall(self) -> bool:
        """Есть ли хотя бы одна тающая стена продавцов."""
        return len(self.melting_ask_walls) > 0

    @property
    def strongest_bid_wall(self) -> Optional[Wall]:
        """Наибольшая стена покупателей по объёму."""
        return max(self.bid_walls, key=lambda w: w.volume, default=None)

    @property
    def strongest_ask_wall(self) -> Optional[Wall]:
        """Наибольшая стена продавцов по объёму."""
        return max(self.ask_walls, key=lambda w: w.volume, default=None)

    def __repr__(self) -> str:
        return (f"OrderBook({self.symbol} mid={self.mid_price:.4f}, "
                f"imb={self.imbalance:.3f}, "
                f"bid_walls={len(self.bid_walls)}, ask_walls={len(self.ask_walls)})")


@dataclass
class Signal:
    """Торговый сигнал от decision.py.

    Attributes:
        direction:    Направление (LONG / SHORT / NONE).
        reason:       Причина сигнала (enum SignalReason).
        confidence:   Уверенность [0..100].
        entry_price:  Предполагаемая цена входа.
        stop_loss:    Уровень стоп-лосса.
        take_profit:  Уровень тейк-профита.
        timestamp:    Unix-timestamp генерации.
        symbol:       Торговый символ.
        metadata:     Дополнительные данные для логирования.
    """
    direction: Direction
    reason: SignalReason
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    timestamp: float = field(default_factory=time.time)
    symbol: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def risk_reward_ratio(self) -> float:
        """Соотношение риск/доходность позиции."""
        if self.direction == Direction.LONG:
            risk = abs(self.entry_price - self.stop_loss)
            reward = abs(self.take_profit - self.entry_price)
        elif self.direction == Direction.SHORT:
            risk = abs(self.stop_loss - self.entry_price)
            reward = abs(self.entry_price - self.take_profit)
        else:
            return 0.0
        return reward / risk if risk > 0 else 0.0

    @property
    def is_valid(self) -> bool:
        """Базовая проверка корректности сигнала."""
        return (self.direction != Direction.NONE
                and self.confidence > 0
                and self.entry_price > 0
                and self.stop_loss > 0
                and self.take_profit > 0
                and self.stop_loss != self.take_profit)

    def __repr__(self) -> str:
        return (f"Signal({self.direction.value} {self.symbol} "
                f"conf={self.confidence:.1f}%, entry={self.entry_price:.4f}, "
                f"sl={self.stop_loss:.4f}, tp={self.take_profit:.4f}, "
                f"rr={self.risk_reward_ratio:.2f})")


@dataclass
class Position:
    """Открытая торговая позиция.

    Attributes:
        id:                  Уникальный идентификатор (UUID).
        symbol:              Торговый символ.
        direction:           Направление позиции.
        entry_price:         Фактическая цена входа.
        quantity:            Объём в базовой валюте.
        stop_loss:           Уровень стоп-лосса.
        take_profit:         Уровень тейк-профита.
        entry_order_id:      ID входного ордера на бирже.
        sl_order_id:         ID ордера стоп-лосса.
        tp_order_id:         ID ордера тейк-профита.
        opened_at:           Unix-timestamp открытия.
        signal:              Исходный сигнал открытия.
        current_price:       Текущая рыночная цена.
        commission_paid:     Уплаченные комиссии.
        max_favorable_price: Пиковая благоприятная цена (для трейлинг-стопа).
    """
    id: str
    symbol: str
    direction: Direction
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    entry_order_id: str = ""
    sl_order_id: str = ""
    tp_order_id: str = ""
    opened_at: float = field(default_factory=time.time)
    signal: Optional[Signal] = None
    current_price: float = 0.0
    commission_paid: float = 0.0
    max_favorable_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        """Нереализованная прибыль/убыток в котируемой валюте."""
        if self.current_price <= 0:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.current_price) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        """Нереализованная прибыль/убыток в %."""
        cost = self.entry_price * self.quantity
        return self.unrealized_pnl / cost * 100.0 if cost > 0 else 0.0

    @property
    def age_seconds(self) -> float:
        """Время жизни позиции в секундах."""
        return time.time() - self.opened_at

    @property
    def notional_value(self) -> float:
        """Номинальная стоимость позиции."""
        return self.entry_price * self.quantity

    @property
    def max_drawdown_pct(self) -> float:
        """Максимальная просадка от пика в % (для трейлинг-стопа)."""
        if self.max_favorable_price <= 0 or self.current_price <= 0:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.max_favorable_price - self.current_price) / self.max_favorable_price * 100
        else:
            return (self.current_price - self.max_favorable_price) / self.max_favorable_price * 100

    def __repr__(self) -> str:
        return (f"Position(id={self.id[:8]}, {self.direction.value} {self.symbol}, "
                f"qty={self.quantity:.6f}, entry={self.entry_price:.4f}, "
                f"pnl={self.unrealized_pnl:+.4f} [{self.unrealized_pnl_pct:+.2f}%], "
                f"age={self.age_seconds:.0f}s)")


@dataclass
class ClosedTrade:
    """Завершённая сделка. Сохраняется в SQLite для статистики.

    Attributes:
        id:                  Уникальный ID (совпадает с Position.id).
        symbol:              Торговый символ.
        direction:           Направление.
        entry_price:         Цена входа.
        exit_price:          Цена выхода.
        quantity:            Объём позиции.
        pnl:                 Реализованный PnL.
        pnl_pct:             PnL в %.
        exit_reason:         Причина закрытия.
        signal_reason:       Причина сигнала.
        confidence:          Уверенность исходного сигнала.
        duration_sec:        Длительность в секундах.
        opened_at:           Timestamp открытия.
        closed_at:           Timestamp закрытия.
        commission:          Суммарные комиссии.
        net_pnl:             PnL за вычетом комиссий.
        max_favorable_price: Пиковая благоприятная цена (MFE).
        max_adverse_price:   Пиковая неблагоприятная цена (MAE).
    """
    id: str
    symbol: str
    direction: Direction
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: ExitReason
    signal_reason: SignalReason
    confidence: float
    duration_sec: float
    opened_at: float
    closed_at: float = field(default_factory=time.time)
    commission: float = 0.0
    net_pnl: float = 0.0
    max_favorable_price: float = 0.0
    max_adverse_price: float = 0.0

    @property
    def is_winner(self) -> bool:
        """True если сделка прибыльная."""
        return self.net_pnl > 0

    @property
    def mae_pct(self) -> float:
        """Maximum Adverse Excursion в %."""
        if self.entry_price <= 0:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.entry_price - self.max_adverse_price) / self.entry_price * 100
        else:
            return (self.max_adverse_price - self.entry_price) / self.entry_price * 100

    @property
    def mfe_pct(self) -> float:
        """Maximum Favorable Excursion в %."""
        if self.entry_price <= 0:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.max_favorable_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - self.max_favorable_price) / self.entry_price * 100

    def __repr__(self) -> str:
        return (f"ClosedTrade(id={self.id[:8]}, {self.direction.value} {self.symbol}, "
                f"pnl={self.net_pnl:+.4f} [{self.pnl_pct:+.2f}%], "
                f"reason={self.exit_reason.value}, dur={self.duration_sec:.0f}s)")


@dataclass
class DailyStats:
    """Суточная торговая статистика. Сохраняется в SQLite.

    Attributes:
        date:                Дата (YYYY-MM-DD).
        total_trades:        Сделок за день.
        winning_trades:      Прибыльных сделок.
        losing_trades:       Убыточных сделок.
        total_pnl:           Суммарный PnL.
        total_commission:    Суммарные комиссии.
        net_pnl:             Чистый PnL.
        win_rate:            Процент выигрышей.
        avg_win:             Средний выигрыш.
        avg_loss:            Средний проигрыш.
        profit_factor:       Суммарная прибыль / суммарный убыток.
        max_drawdown:        Максимальная просадка за день.
        sharpe_ratio:        Коэффициент Шарпа.
        starting_balance:    Баланс в начале дня.
        ending_balance:      Баланс в конце дня.
        signals_generated:   Сигналов сгенерировано.
        signals_executed:    Сигналов исполнено.
    """
    date: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_commission: float = 0.0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    starting_balance: float = 0.0
    ending_balance: float = 0.0
    signals_generated: int = 0
    signals_executed: int = 0

    @property
    def execution_rate(self) -> float:
        """Процент сигналов, ставших позициями."""
        return self.signals_executed / self.signals_generated * 100 if self.signals_generated else 0.0

    @property
    def return_pct(self) -> float:
        """Дневная доходность в %."""
        return self.net_pnl / self.starting_balance * 100 if self.starting_balance else 0.0

    def __repr__(self) -> str:
        return (f"DailyStats({self.date}: trades={self.total_trades}, "
                f"win_rate={self.win_rate:.1f}%, net_pnl={self.net_pnl:+.2f})")


@dataclass
class Tick:
    """Один тик рынка (отдельная сделка на бирже).

    Attributes:
        symbol:     Торговый символ.
        price:      Цена сделки.
        quantity:   Объём сделки.
        side:       'buy' или 'sell'.
        trade_id:   ID сделки на бирже.
        timestamp:  Unix-timestamp сделки.
    """
    symbol: str
    price: float
    quantity: float
    side: str
    trade_id: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def value(self) -> float:
        """Стоимость тика в котируемой валюте."""
        return self.price * self.quantity

    def __repr__(self) -> str:
        return f"Tick({self.symbol} {self.side} {self.quantity:.6f}@{self.price:.4f})"


@dataclass
class Candle:
    """Свеча OHLCV. Используется в стратегиях с техническим анализом.

    Attributes:
        symbol:              Торговый символ.
        interval:            Таймфрейм ('1m', '5m', '15m', '1h', '4h', '1d').
        open_time:           Unix-timestamp открытия.
        close_time:          Unix-timestamp закрытия.
        open:                Цена открытия.
        high:                Максимум.
        low:                 Минимум.
        close:               Цена закрытия.
        volume:              Объём (базовая валюта).
        quote_volume:        Объём (котируемая валюта).
        trades:              Количество сделок.
        taker_buy_vol:       Объём агрессивных покупок.
        taker_buy_quote_vol: Объём агрессивных покупок в котируемой валюте.
        is_closed:           True если свеча закрыта.
    """
    symbol: str
    interval: str
    open_time: float
    close_time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades: int = 0
    taker_buy_vol: float = 0.0
    taker_buy_quote_vol: float = 0.0
    is_closed: bool = True

    @property
    def is_bullish(self) -> bool:
        """True если закрытие выше открытия."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """True если закрытие ниже открытия."""
        return self.close < self.open

    @property
    def body_size(self) -> float:
        """Абсолютный размер тела свечи."""
        return abs(self.close - self.open)

    @property
    def body_pct(self) -> float:
        """Размер тела в % от открытия."""
        return self.body_size / self.open * 100 if self.open > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        """Верхний фитиль."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Нижний фитиль."""
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        """Диапазон high - low."""
        return self.high - self.low

    @property
    def buy_pressure(self) -> float:
        """Давление покупателей: taker_buy_vol / volume."""
        return self.taker_buy_vol / self.volume if self.volume > 0 else 0.5

    def __repr__(self) -> str:
        trend = "UP" if self.is_bullish else "DN"
        return (f"Candle({trend} {self.symbol} {self.interval} "
                f"o={self.open:.4f} h={self.high:.4f} l={self.low:.4f} c={self.close:.4f})")


# ---------------------------------------------------------------------------
# Конфигурации
# ---------------------------------------------------------------------------


@dataclass
class MexcConfig:
    """Конфигурация подключения к бирже MEXC.

    Attributes:
        api_key:               API-ключ (из переменной окружения MEXC_API_KEY).
        api_secret:            API-секрет (из MEXC_API_SECRET).
        testnet:               Использовать тестовую сеть.
        rest_url:              Базовый URL REST API.
        ws_url:                URL WebSocket API.
        depth_limit:           Уровней стакана (5/10/20).
        recv_window:           Допустимое отклонение времени (мс).
        rate_limit_req_per_sec: Лимит запросов в секунду.
        max_retry:             Максимум повторных попыток REST-запроса.
        retry_delay_base:      Базовая задержка перед повтором (с).
        ws_ping_interval:      Интервал ping по WebSocket (с).
        ws_reconnect_max_age:  Принудительное переподключение через N секунд.
    """
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    rest_url: str = "https://api.mexc.com"
    ws_url: str = "wss://wbs-api.mexc.com/ws"
    depth_limit: int = 20
    recv_window: int = 5000
    rate_limit_req_per_sec: float = 10.0
    max_retry: int = 3
    retry_delay_base: float = 1.0
    ws_ping_interval: float = 20.0
    ws_reconnect_max_age: float = 86400.0


@dataclass
class AnalyzerConfig:
    """Настройки алгоритма анализа стакана.

    Attributes:
        wall_threshold:        Порог: объём > avg * wall_threshold → стена.
        melt_speed_threshold:  Скорость таяния (%/с) для признания стены тающей.
        imbalance_levels:      Количество уровней K для расчёта дисбаланса.
        history_size:          Размер истории объёмов уровня.
        min_wall_depth:        Минимальная глубина от лучшей цены (%).
        max_wall_depth:        Максимальная глубина — дальние стены игнорируются (%).
        spread_max_pct:        Максимальный спред для работы бота (%).
    """
    wall_threshold: float = 5.0
    melt_speed_threshold: float = 10.0
    imbalance_levels: int = 10
    history_size: int = 10
    min_wall_depth: float = 0.05
    max_wall_depth: float = 2.0
    spread_max_pct: float = 0.1


@dataclass
class RiskConfig:
    """Параметры управления рисками.

    Attributes:
        risk_per_trade_percent:   Риск на сделку (% от баланса).
        max_daily_loss_percent:   Максимальный дневной убыток (%).
        max_consecutive_losses:   Лимит убытков подряд.
        stop_loss_percent:        Размер стоп-лосса (% от цены входа).
        take_profit_percent:      Размер тейк-профита (% от цены входа).
        max_open_positions:       Максимум одновременных позиций.
        min_risk_reward:          Минимальное соотношение риск/доходность.
        max_position_age_sec:     Принудительное закрытие через N секунд.
        trailing_stop_enabled:    Включить трейлинг-стоп.
        trailing_stop_pct:        Расстояние трейлинг-стопа от пика (%).
        commission_rate:          Комиссия биржи (% от объёма).
    """
    risk_per_trade_percent: float = 1.0
    max_daily_loss_percent: float = 5.0
    max_consecutive_losses: int = 3
    stop_loss_percent: float = 2.0
    take_profit_percent: float = 3.0
    max_open_positions: int = 1
    min_risk_reward: float = 1.5
    max_position_age_sec: float = 3600.0
    trailing_stop_enabled: bool = False
    trailing_stop_pct: float = 1.0
    commission_rate: float = 0.001


@dataclass
class TelegramConfig:
    """Конфигурация Telegram-уведомлений.

    Attributes:
        enabled:           Включить уведомления.
        bot_token:         Токен бота (из BotFather).
        chat_id:           ID чата.
        notify_signals:    Уведомлять о сигналах.
        notify_trades:     Уведомлять об открытии/закрытии позиции.
        notify_errors:     Уведомлять об ошибках.
        daily_report_hour: Час ежедневного отчёта (UTC 0-23).
        max_message_len:   Максимальная длина сообщения.
    """
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    notify_signals: bool = True
    notify_trades: bool = True
    notify_errors: bool = True
    daily_report_hour: int = 23
    max_message_len: int = 4096


@dataclass
class LoggingConfig:
    """Конфигурация логирования и хранения данных.

    Attributes:
        level:          Уровень логирования ('DEBUG', 'INFO', 'WARNING', 'ERROR').
        log_file:       Путь к файлу логов.
        db_file:        Путь к SQLite БД.
        rotation:       Размер файла для ротации (e.g. '10 MB').
        retention:      Срок хранения (e.g. '30 days').
        log_to_console: Дублировать в консоль.
        json_logs:      JSON-формат для ELK/Loki.
    """
    level: str = "INFO"
    log_file: str = "logs/bot.log"
    db_file: str = "data/trades.db"
    rotation: str = "10 MB"
    retention: str = "30 days"
    log_to_console: bool = True
    json_logs: bool = False


@dataclass
class BotConfig:
    """Главная конфигурация бота. Объединяет все суб-конфиги.

    Attributes:
        symbol:                 Торговый символ (e.g. 'BTCUSDT').
        confidence_threshold:   Минимальная уверенность сигнала [0..100].
        loop_interval_seconds:  Интервал главного цикла в секундах.
        mexc:                   Конфигурация биржи.
        analyzer:               Конфигурация анализатора.
        risk:                   Конфигурация риск-менеджера.
        telegram:               Конфигурация Telegram.
        logging:                Конфигурация логирования.
        dry_run:                Режим симуляции (без реальных ордеров).
        version:                Версия конфига.
    """
    symbol: str = "BTCUSDT"
    confidence_threshold: float = 70.0
    loop_interval_seconds: float = 1.0
    mexc: MexcConfig = field(default_factory=MexcConfig)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dry_run: bool = False
    version: str = "1.0"

    def __repr__(self) -> str:
        return (f"BotConfig(symbol={self.symbol}, "
                f"confidence_thr={self.confidence_threshold}, "
                f"dry_run={self.dry_run})")
