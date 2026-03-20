"""
demo.py — Демо-режим CONFI Scalper.

РЕАЛЬНЫЕ данные с биржи MEXC (публичный WebSocket, API ключи НЕ нужны).
ВИРТУАЛЬНЫЕ сделки — реальных ордеров нет, деньги не тратятся.

Что происходит:
  - Подключается к MEXC WebSocket (публичный, без авторизации)
  - Получает реальный стакан ордеров в режиме реального времени
  - Анализирует стены, дисбаланс, таяние
  - Симулирует сделки по реальным ценам (виртуальный баланс)
  - Показывает live-дашборд в терминале

Запуск:
    python demo.py                    # BTCUSDT, баланс 1000 USDT
    python demo.py --symbol ETHUSDT   # другой символ
    python demo.py --balance 5000     # другой стартовый баланс
    python demo.py --confidence 60    # ниже порог уверенности

Требования (ТОЛЬКО публичные, без API ключей):
    pip install websockets aiohttp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Coroutine, Deque, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────
# Минимальные вспомогательные структуры
# (без импорта тяжёлых зависимостей)
# ──────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.makedirs("logs", exist_ok=True)
# Настройка логов — тихий режим для чистого дашборда
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("logs/demo.log", encoding="utf-8")],
)


# ──────────────────────────────────────────────
# Константы MEXC WebSocket
# ──────────────────────────────────────────────

MEXC_WS_URL = "wss://wbs-api.mexc.com/ws"
MEXC_REST_URL = "https://api.mexc.com"

# Если WS заблокирован (geo-block) — автоматически переключаемся на REST-поллинг
WS_BLOCKED_MSG = "Blocked"
REST_POLL_INTERVAL = 1.0   # секунд между REST-запросами стакана

# Цвета для терминала
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    DIM    = "\033[2m"
    BG_RED   = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_BLUE  = "\033[44m"

def clr(text: str, color: str) -> str:
    return f"{color}{text}{C.RESET}"

def pnl_color(val: float) -> str:
    if val > 0:   return C.GREEN
    if val < 0:   return C.RED
    return C.WHITE


# ──────────────────────────────────────────────
# Виртуальная позиция
# ──────────────────────────────────────────────

class VirtualPosition:
    """Виртуальная торговая позиция. Без реальных ордеров."""

    def __init__(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        reason: str,
        confidence: float,
    ):
        self.id          = str(uuid.uuid4())[:8]
        self.symbol      = symbol
        self.direction   = direction   # "LONG" | "SHORT"
        self.entry_price = entry_price
        self.quantity    = quantity
        self.stop_loss   = stop_loss
        self.take_profit = take_profit
        self.reason      = reason
        self.confidence  = confidence
        self.opened_at   = time.time()
        self.current_price = entry_price
        self.peak_price  = entry_price
        self.status      = "OPEN"

    @property
    def unrealized_pnl(self) -> float:
        if self.direction == "LONG":
            return (self.current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.current_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        cost = self.entry_price * self.quantity
        return self.unrealized_pnl / cost * 100 if cost > 0 else 0.0

    @property
    def age(self) -> float:
        return time.time() - self.opened_at

    def update_price(self, price: float) -> Optional[str]:
        """Обновить цену. Вернуть 'SL'/'TP'/None."""
        self.current_price = price
        if self.direction == "LONG":
            self.peak_price = max(self.peak_price, price)
            if price <= self.stop_loss:   return "SL"
            if price >= self.take_profit: return "TP"
        else:
            self.peak_price = min(self.peak_price, price)
            if price >= self.stop_loss:   return "SL"
            if price <= self.take_profit: return "TP"
        return None


# ──────────────────────────────────────────────
# Замкнутая сделка
# ──────────────────────────────────────────────

class ClosedTrade:
    def __init__(self, pos: VirtualPosition, exit_price: float, exit_reason: str, commission_rate: float = 0.001):
        self.pos         = pos
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        self.closed_at   = time.time()
        if pos.direction == "LONG":
            raw_pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            raw_pnl = (pos.entry_price - exit_price) * pos.quantity
        # Комиссия только 0.1% от объёма (не удваивать)
        commission = pos.entry_price * pos.quantity * commission_rate
        self.net_pnl   = raw_pnl - commission
        self.pnl_pct   = raw_pnl / (pos.entry_price * pos.quantity) * 100 if pos.entry_price * pos.quantity > 0 else 0
        self.duration  = self.closed_at - pos.opened_at
        self.is_win    = self.net_pnl > 0


# ──────────────────────────────────────────────
# Анализатор стакана (встроенный, без импортов)
# ──────────────────────────────────────────────

class LiveAnalyzer:
    """
    Анализирует реальный стакан MEXC.
    Включает: Delta, Momentum, Anti-Flat, Anti-Spoofing,
    Volume Spike, Market Direction, Signal Strength.
    """

    def __init__(
        self,
        wall_threshold: float = 5.0,
        melt_speed_threshold: float = 10.0,
        imbalance_levels: int = 10,
        history_size: int = 10,
        min_wall_depth_pct: float = 0.03,
        max_wall_depth_pct: float = 2.0,
    ):
        self.wall_threshold       = wall_threshold
        self.melt_speed           = melt_speed_threshold
        self.imb_levels           = imbalance_levels
        self.history_size         = history_size
        self.min_depth            = min_wall_depth_pct
        self.max_depth            = max_wall_depth_pct

        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._bid_hist: Dict[float, Deque] = defaultdict(lambda: deque(maxlen=history_size))
        self._ask_hist: Dict[float, Deque] = defaultdict(lambda: deque(maxlen=history_size))
        self._bid_ts:   Dict[float, float] = {}
        self._ask_ts:   Dict[float, float] = {}

        # Основные метрики стакана
        self.best_bid    = 0.0
        self.best_ask    = 0.0
        self.mid_price   = 0.0
        self.spread_pct  = 0.0
        self.imbalance   = 1.0
        self.bid_walls: List[dict] = []
        self.ask_walls: List[dict] = []
        self.melting_ask: List[dict] = []
        self.melting_bid: List[dict] = []
        self.total_bid_vol = 0.0
        self.total_ask_vol = 0.0
        self.update_count  = 0
        self.last_update   = 0.0

        # DELTA — разница buy/sell объёмов
        self.delta_1s   = 0.0   # дельта за 1 сек
        self.delta_3s   = 0.0   # дельта за 3 сек
        self.delta_5s   = 0.0   # дельта за 5 сек
        self.avg_delta  = 0.0   # средняя дельта
        self._delta_history: Deque = deque(maxlen=30)
        self._vol_history: Deque = deque(maxlen=30)  # история объёмов стакана

        # MOMENTUM — скорость изменения цены
        self.momentum_1s  = 0.0
        self.momentum_3s  = 0.0
        self.momentum_5s  = 0.0
        self._price_ts_history: Deque = deque(maxlen=60)  # (timestamp, price)

        # VOLUME SPIKE — всплеск объёма
        self.volume_spike     = False
        self.volume_spike_dir = ""  # "buy" или "sell"
        self._avg_vol         = 0.0

        # MARKET DIRECTION — направление рынка
        self.market_direction = "flat"  # "up", "down", "flat"
        self.market_strength  = 0.0    # 0-100

        # ANTI-FLAT — флаг флэта
        self.is_flat = True

        # SIGNAL STRENGTH — итоговая сила сигнала
        self.signal_score = 0.0  # 0-100

        # ANTI-SPOOFING — обнаружение фейковых стен
        self.spoofing_detected = False
        self._wall_vol_history: Dict[float, Deque] = defaultdict(lambda: deque(maxlen=5))
        self._lock = None  # создаётся при первом вызове process() внутри event loop

    async def process(self, data: dict) -> bool:
        """Обработать ws-сообщение стакана. Вернуть True если данные обновлены."""
        now = time.time()
        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])

        for item in bids_raw:
            try:
                p, v = float(item[0]), float(item[1])
                if v == 0:
                    self._bids.pop(p, None)
                    self._bid_hist.pop(p, None)
                    self._bid_ts.pop(p, None)
                else:
                    prev = self._bids.get(p, 0.0)
                    self._bid_hist[p].append(prev if prev > 0 else v)
                    self._bids[p] = v
                    if p not in self._bid_ts:
                        self._bid_ts[p] = now
            except Exception:
                continue

        for item in asks_raw:
            try:
                p, v = float(item[0]), float(item[1])
                if v == 0:
                    self._asks.pop(p, None)
                    self._ask_hist.pop(p, None)
                    self._ask_ts.pop(p, None)
                else:
                    prev = self._asks.get(p, 0.0)
                    self._ask_hist[p].append(prev if prev > 0 else v)
                    self._asks[p] = v
                    if p not in self._ask_ts:
                        self._ask_ts[p] = now
            except Exception:
                continue

        if not self._bids or not self._asks:
            return False

        sorted_bids = sorted(self._bids.items(), reverse=True)
        sorted_asks = sorted(self._asks.items())

        best_bid = sorted_bids[0][0]
        best_ask = sorted_asks[0][0]
        if best_bid >= best_ask:
            return False

        self.best_bid   = best_bid
        self.best_ask   = best_ask
        self.mid_price  = (best_bid + best_ask) / 2
        self.spread_pct = (best_ask - best_bid) / self.mid_price * 100

        k = min(self.imb_levels, len(sorted_bids), len(sorted_asks))
        bid_vol = sum(v for _, v in sorted_bids[:k])
        ask_vol = sum(v for _, v in sorted_asks[:k])
        self.total_bid_vol = bid_vol
        self.total_ask_vol = ask_vol
        self.imbalance = bid_vol / ask_vol if ask_vol > 0 else 1.0

        bid_avg = bid_vol / k if k > 0 else 0
        ask_avg = ask_vol / k if k > 0 else 0
        self.bid_walls  = self._detect_walls(sorted_bids[:k], bid_avg, best_bid, "bid", now)
        self.ask_walls  = self._detect_walls(sorted_asks[:k], ask_avg, best_ask, "ask", now)
        self.melting_bid = [w for w in self.bid_walls if w["melting"]]
        self.melting_ask = [w for w in self.ask_walls if w["melting"]]

        # ── DELTA ──
        delta_now = bid_vol - ask_vol
        self._delta_history.append((now, delta_now))
        self._calc_delta(now)

        # ── MOMENTUM ──
        self._price_ts_history.append((now, self.mid_price))
        self._calc_momentum(now)

        # ── VOLUME SPIKE ──
        total_vol = bid_vol + ask_vol
        self._vol_history.append(total_vol)
        self._calc_volume_spike(total_vol, bid_vol, ask_vol)

        # ── ANTI-FLAT ──
        self._calc_anti_flat()

        # ── MARKET DIRECTION ──
        self._calc_market_direction()

        # ── ANTI-SPOOFING ──
        self._calc_anti_spoofing(sorted_bids[:k], sorted_asks[:k], now)

        # ── SIGNAL SCORE ──
        self._calc_signal_score()

        self.update_count += 1
        self.last_update = now
        return True

    def _calc_delta(self, now: float):
        """Рассчитать дельту за 1/3/5 секунд."""
        vals_1s, vals_3s, vals_5s = [], [], []
        for ts, d in self._delta_history:
            age = now - ts
            if age <= 1: vals_1s.append(d)
            if age <= 3: vals_3s.append(d)
            if age <= 5: vals_5s.append(d)
        self.delta_1s = sum(vals_1s) / len(vals_1s) if vals_1s else 0.0
        self.delta_3s = sum(vals_3s) / len(vals_3s) if vals_3s else 0.0
        self.delta_5s = sum(vals_5s) / len(vals_5s) if vals_5s else 0.0
        all_deltas = [d for _, d in self._delta_history]
        self.avg_delta = sum(all_deltas) / len(all_deltas) if all_deltas else 0.0

    def _calc_momentum(self, now: float):
        """Рассчитать моментум за 1/3/5 секунд."""
        if len(self._price_ts_history) < 2:
            return
        prices = list(self._price_ts_history)
        cur_price = prices[-1][1]
        def get_price_ago(secs):
            for ts, p in reversed(prices):
                if now - ts >= secs:
                    return p
            return prices[0][1]
        p1 = get_price_ago(1)
        p3 = get_price_ago(3)
        p5 = get_price_ago(5)
        self.momentum_1s = (cur_price - p1) / p1 * 100 if p1 > 0 else 0.0
        self.momentum_3s = (cur_price - p3) / p3 * 100 if p3 > 0 else 0.0
        self.momentum_5s = (cur_price - p5) / p5 * 100 if p5 > 0 else 0.0

    def _calc_volume_spike(self, total_vol: float, bid_vol: float, ask_vol: float):
        """Обнаружить всплеск объёма."""
        if len(self._vol_history) < 5:
            self.volume_spike = False
            return
        vols = list(self._vol_history)
        self._avg_vol = sum(vols[:-1]) / (len(vols) - 1)
        if self._avg_vol > 0 and total_vol > self._avg_vol * 2.0:
            self.volume_spike = True
            self.volume_spike_dir = "buy" if bid_vol > ask_vol else "sell"
        else:
            self.volume_spike = False
            self.volume_spike_dir = ""

    def _calc_anti_flat(self):
        """Определить флэт по моментуму и спреду."""
        mom_abs = abs(self.momentum_3s)
        self.is_flat = mom_abs < 0.03 and self.spread_pct < 0.005

    def _calc_market_direction(self):
        """Определить направление рынка по истории цен."""
        if len(self._price_ts_history) < 10:
            self.market_direction = "flat"
            self.market_strength = 0.0
            return
        prices = [p for _, p in self._price_ts_history]
        first = prices[0]
        last = prices[-1]
        change_pct = (last - first) / first * 100 if first > 0 else 0
        # Считаем сколько свечей вверх vs вниз
        ups = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i-1])
        downs = len(prices) - 1 - ups
        if change_pct > 0.03 and ups > downs * 1.5:
            self.market_direction = "up"
            self.market_strength = min(abs(change_pct) * 20, 100)
        elif change_pct < -0.03 and downs > ups * 1.5:
            self.market_direction = "down"
            self.market_strength = min(abs(change_pct) * 20, 100)
        else:
            self.market_direction = "flat"
            self.market_strength = 0.0

    def _calc_anti_spoofing(self, bids: list, asks: list, now: float):
        """Обнаружить спуфинг — стены которые быстро появляются и исчезают."""
        self.spoofing_detected = False
        # Проверяем bid стены
        for price, vol in bids:
            hist = self._wall_vol_history[price]
            hist.append(vol)
            if len(hist) >= 3:
                # Если объём резко вырос и быстро упал — спуфинг
                max_v = max(hist)
                min_v = min(hist)
                if max_v > 0 and min_v / max_v < 0.3:
                    self.spoofing_detected = True
                    return
        # Проверяем ask стены
        for price, vol in asks:
            hist = self._wall_vol_history[price + 1000000]  # разделяем bid/ask ключи
            hist.append(vol)
            if len(hist) >= 3:
                max_v = max(hist)
                min_v = min(hist)
                if max_v > 0 and min_v / max_v < 0.3:
                    self.spoofing_detected = True
                    return

    def _calc_signal_score(self):
        """Рассчитать итоговую силу сигнала 0-100."""
        score = 0.0
        # Дисбаланс стакана (0-25)
        imb_score = min(abs(self.imbalance - 1) * 12, 25)
        score += imb_score
        # Моментум (0-25)
        mom_score = min(abs(self.momentum_3s) * 250, 25)
        score += mom_score
        # Дельта (0-20)
        if self.avg_delta != 0:
            delta_ratio = abs(self.delta_3s / self.avg_delta) if self.avg_delta != 0 else 0
            score += min(delta_ratio * 10, 20)
        # Тающие стены (0-15)
        if self.melting_ask or self.melting_bid:
            score += 15
        # Volume spike (0-10)
        if self.volume_spike:
            score += 10
        # Штрафы
        if self.is_flat:
            score *= 0.3
        if self.spoofing_detected:
            score *= 0.5
        self.signal_score = min(score, 100)

    def _detect_walls(
        self, levels: list, avg_vol: float, best_price: float, side: str, now: float
    ) -> List[dict]:
        walls = []
        threshold = avg_vol * self.wall_threshold
        if threshold <= 0:
            return walls
        hist_map = self._bid_hist if side == "bid" else self._ask_hist
        ts_map   = self._bid_ts   if side == "bid" else self._ask_ts

        for price, volume in levels:
            if volume < threshold:
                continue
            depth_pct = abs(price - best_price) / best_price * 100
            if not (self.min_depth <= depth_pct <= self.max_depth):
                continue

            hist = list(hist_map.get(price, []))
            prev_vol = hist[-1] if hist else volume
            elapsed  = now - ts_map.get(price, now)
            if elapsed > 0 and prev_vol > 0 and prev_vol > volume:
                melt_rate = (prev_vol - volume) / prev_vol * 100 / elapsed
            else:
                melt_rate = 0.0

            walls.append({
                "side":    side,
                "price":   price,
                "volume":  volume,
                "depth":   depth_pct,
                "melt_rate": melt_rate,
                "melting": melt_rate >= self.melt_speed,
            })
        return walls

    def generate_signal(
        self,
        confidence_threshold: float = 70.0,
        imb_long_thr: float = 2.0,
        imb_short_thr: float = 0.5,
        min_rr: float = 2.0,
        sl_pct: float = 2.0,
        tp_pct: float = 3.0,
    ) -> Optional[dict]:
        """
        ФИНАЛЬНОЕ РЕШЕНИЕ — все фильтры применяются последовательно:
        1. Anti-Flat блок
        2. Anti-Spoofing блок
        3. Спред фильтр
        4. Momentum фильтр
        5. Определение направления
        6. Delta подтверждение
        7. Volume spike бонус
        8. Расчёт уверенности
        """
        price = self.mid_price
        if price <= 0:
            return None

        # ── БЛОК 1: ANTI-FLAT ──
        if self.is_flat:
            return None

        # ── БЛОК 2: СПРЕД ──
        if self.spread_pct > 0.05:
            return None

        # ── БЛОК 3: ANTI-SPOOFING ──
        if self.spoofing_detected:
            return None

        # ── БЛОК 4: ДУБЛИ — только если нет открытой позиции (проверяется снаружи) ──

        # ── БЛОК 5: ТАЮЩИЕ СТЕНЫ (приоритет 1) ──
        if self.melting_ask and self.momentum_3s > 0.02:
            wall = max(self.melting_ask, key=lambda w: w["melt_rate"])
            conf = min(wall["melt_rate"] / 10.0 * 50 + self.signal_score * 0.5, 100)
            if conf >= confidence_threshold:
                return self._make_signal("LONG", "melting_ask_wall", conf, price, sl_pct, tp_pct, min_rr, wall)

        if self.melting_bid and self.momentum_3s < -0.02:
            wall = max(self.melting_bid, key=lambda w: w["melt_rate"])
            conf = min(wall["melt_rate"] / 10.0 * 50 + self.signal_score * 0.5, 100)
            if conf >= confidence_threshold:
                return self._make_signal("SHORT", "melting_bid_wall", conf, price, sl_pct, tp_pct, min_rr, wall)

        # ── БЛОК 6: ДИСБАЛАНС + MOMENTUM + DELTA ──
        # LONG: bid > ask, momentum вверх, delta положительная
        if (self.imbalance >= imb_long_thr
                and self.momentum_3s >= 0.03
                and self.delta_3s > 0
                and not self.ask_walls
                and self.market_direction in ("up", "flat")):
            base_conf = min((self.imbalance - imb_long_thr) * 12 + 55, 85)
            # Бонусы
            if self.volume_spike and self.volume_spike_dir == "buy":
                base_conf = min(base_conf + 10, 95)
            if self.momentum_3s > 0.05:
                base_conf = min(base_conf + 5, 95)
            if self.delta_3s > self.avg_delta * 1.5:
                base_conf = min(base_conf + 5, 95)
            if base_conf >= confidence_threshold:
                reason = "bid_imbalance+momentum"
                if self.volume_spike:
                    reason += "+vol_spike"
                return self._make_signal("LONG", reason, base_conf, price, sl_pct, tp_pct, min_rr)

        # SHORT: ask > bid, momentum вниз, delta отрицательная
        if (self.imbalance <= imb_short_thr
                and self.momentum_3s <= -0.03
                and self.delta_3s < 0
                and not self.bid_walls
                and self.market_direction in ("down", "flat")):
            base_conf = min((imb_short_thr - self.imbalance) / imb_short_thr * 25 + 55, 85)
            if self.volume_spike and self.volume_spike_dir == "sell":
                base_conf = min(base_conf + 10, 95)
            if self.momentum_3s < -0.05:
                base_conf = min(base_conf + 5, 95)
            if self.delta_3s < self.avg_delta * -1.5:
                base_conf = min(base_conf + 5, 95)
            if base_conf >= confidence_threshold:
                reason = "ask_imbalance+momentum"
                if self.volume_spike:
                    reason += "+vol_spike"
                return self._make_signal("SHORT", reason, base_conf, price, sl_pct, tp_pct, min_rr)

        return None

    @staticmethod
    def _make_signal(
        direction: str, reason: str, confidence: float, price: float,
        sl_pct: float, tp_pct: float, min_rr: float, wall: dict = None
    ) -> Optional[dict]:
        mul_sl = 1 - sl_pct / 100 if direction == "LONG" else 1 + sl_pct / 100
        mul_tp = 1 + tp_pct / 100 if direction == "LONG" else 1 - tp_pct / 100
        sl = price * mul_sl
        tp = price * mul_tp
        risk   = abs(price - sl)
        reward = abs(tp - price)
        rr = reward / risk if risk > 0 else 0
        if rr < min_rr:
            return None
        return {
            "direction":  direction,
            "reason":     reason,
            "confidence": round(confidence, 1),
            "entry":      price,
            "sl":         sl,
            "tp":         tp,
            "rr":         round(rr, 2),
            "wall":       wall,
            "ts":         time.time(),
        }


# ──────────────────────────────────────────────
# Демо-движок
# ──────────────────────────────────────────────

class DemoEngine:
    """
    Главный демо-движок.
    Подключается к реальному MEXC WebSocket,
    торгует виртуально, показывает дашборд.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        balance: float = 1000.0,
        risk_pct: float = 1.0,
        sl_pct: float = 2.0,
        tp_pct: float = 3.0,
        confidence: float = 70.0,
        wall_threshold: float = 5.0,
        melt_threshold: float = 10.0,
        max_daily_loss_pct: float = 5.0,
        max_consec_losses: int = 3,
        commission_rate: float = 0.001,
        depth_level: int = 20,
    ):
        self.symbol          = symbol.upper()
        self.start_balance   = balance
        self.balance         = balance
        self.risk_pct        = risk_pct
        self.sl_pct          = sl_pct
        self.tp_pct          = tp_pct
        self.confidence_thr  = confidence
        self.max_daily_loss  = max_daily_loss_pct
        self.max_consec      = max_consec_losses
        self.commission_rate = commission_rate
        self.depth_level     = depth_level

        self.analyzer = LiveAnalyzer(
            wall_threshold=wall_threshold,
            melt_speed_threshold=melt_threshold,
            imbalance_levels=10,
            history_size=12,
            min_wall_depth_pct=0.02,
            max_wall_depth_pct=3.0,
        )

        # Состояние
        self.position: Optional[VirtualPosition] = None
        self.closed_trades: List[ClosedTrade] = []
        self.signals_generated = 0
        self.last_signal: Optional[dict] = None
        self.consec_losses = 0
        self.daily_pnl = 0.0
        self.start_time = time.time()
        self.ws_messages = 0
        self.running = False
        self.ws_blocked = False
        self.rest_polls = 0

        # Для дашборда: лог последних событий
        self.event_log: deque = deque(maxlen=8)

        # Текущая цена
        self.last_trade_price = 0.0
        self.price_history: deque = deque(maxlen=60)

        # ── RSI ──
        self._rsi_prices: deque = deque(maxlen=15)
        self.rsi = 50.0

        # ── АВТО-ОПТИМИЗАЦИЯ ──
        self._auto_opt_counter = 0
        self._best_reason_stats: Dict[str, dict] = {}

        # ── КОРРЕЛЯЦИЯ С BTC ──
        self._btc_price = 0.0
        self._btc_history: deque = deque(maxlen=30)
        self._btc_direction = "flat"
        self._last_btc_update = 0.0

        # ── СТАТИСТИКА ПО ЧАСАМ ──
        self._hourly_stats: Dict[int, dict] = {}
        self._last_hourly_report = time.time()
        self._last_weekly_report = time.time()

        # ── ДНЕВНОЙ ЛИМИТ СДЕЛОК ──
        self._daily_trade_count = 0
        self._daily_trade_limit = 15
        self._last_day = datetime.now().day

        # ── ПАУЗА ПОСЛЕ СЕРИИ SL ──
        self._sl_pause_until = 0.0

        # ── RSI ──
        self.rsi = 50.0
        self._rsi_gains: deque = deque(maxlen=14)
        self._rsi_losses: deque = deque(maxlen=14)
        self._prev_price_for_rsi = 0.0

        # ── АВТО-ОПТИМИЗАЦИЯ ──
        self._auto_opt_counter = 0   # каждые 10 сделок пересчитываем
        self._win_streak = 0
        self._loss_streak = 0

        # ── КОРРЕЛЯЦИЯ С BTC ──
        self.btc_price = 0.0
        self.btc_momentum = 0.0
        self._btc_history: deque = deque(maxlen=30)
        self._last_btc_fetch = 0.0

        # ── СТАТИСТИКА ПО ПАТТЕРНАМ ──
        self._pattern_stats: Dict[str, List[float]] = {}  # reason -> [pnl, ...]

        # ── УРОВНИ ПОДДЕРЖКИ/СОПРОТИВЛЕНИЯ ──
        self._support_levels: List[float] = []
        self._resistance_levels: List[float] = []
        self._sr_last_update = 0.0

        # ── МУЛЬТИТАЙМФРЕЙМ ──
        self._tf_1m: deque = deque(maxlen=60)   # цены последних 60 секунд
        self._tf_5m: deque = deque(maxlen=60)   # цены каждые 5 сек (5 мин)
        self._tf_15m: deque = deque(maxlen=60)  # цены каждые 15 сек (15 мин)
        self._tf_last_1m = 0.0
        self._tf_last_5m = 0.0
        self._tf_last_15m = 0.0
        self.tf_agreement = "none"  # "long", "short", "none"

        # ── ДИНАМИЧЕСКИЙ РАЗМЕР ПОЗИЦИИ ──
        self._base_risk_pct = risk_pct
        self._streak_multiplier = 1.0  # увеличивается при победах

        # ── ПАМЯТЬ СДЕЛОК МЕЖДУ СЕССИЯМИ ──
        self._history_file = "logs/trade_history.json"
        self._session_start_trades = 0

        # ── ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ──
        self._weekly_trades_start = len(self.closed_trades)
        self._weekly_pnl_start = 0.0

    def _log_event(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.event_log.append(f"{C.DIM}{ts}{C.RESET} {msg}")

    def _update_rsi(self, price: float):
        """Обновить RSI (14 периодов)."""
        if self._prev_price_for_rsi == 0:
            self._prev_price_for_rsi = price
            return
        change = price - self._prev_price_for_rsi
        self._prev_price_for_rsi = price
        if change > 0:
            self._rsi_gains.append(change)
            self._rsi_losses.append(0)
        else:
            self._rsi_gains.append(0)
            self._rsi_losses.append(abs(change))
        if len(self._rsi_gains) >= 14:
            avg_gain = sum(self._rsi_gains) / 14
            avg_loss = sum(self._rsi_losses) / 14
            if avg_loss == 0:
                self.rsi = 100.0
            else:
                rs = avg_gain / avg_loss
                self.rsi = 100 - (100 / (1 + rs))

    def _is_rsi_ok(self, direction: str) -> bool:
        """RSI фильтр: не входить в перекупленность/перепроданность."""
        if direction == "LONG" and self.rsi > 75:
            return False  # перекуплен
        if direction == "SHORT" and self.rsi < 25:
            return False  # перепродан
        return True

    async def _fetch_btc_price(self):
        """Получить цену BTC для корреляции."""
        import urllib.request, json as _json
        now = time.time()
        if now - self._last_btc_fetch < 10:  # обновляем каждые 10 сек
            return
        self._last_btc_fetch = now
        try:
            url = f"{MEXC_REST_URL}/api/v3/ticker/price?symbol=BTCUSDT"
            loop = asyncio.get_event_loop()
            def fetch():
                with urllib.request.urlopen(url, timeout=5) as r:
                    return _json.loads(r.read())
            data = await loop.run_in_executor(None, fetch)
            new_price = float(data.get("price", 0))
            if self.btc_price > 0 and new_price > 0:
                self.btc_momentum = (new_price - self.btc_price) / self.btc_price * 100
                self._btc_history.append(new_price)
            self.btc_price = new_price
        except Exception:
            pass

    def _is_btc_corr_ok(self, direction: str) -> bool:
        """Проверить корреляцию с BTC (только если торгуем не BTC)."""
        if "BTC" in self.symbol or self.btc_price == 0:
            return True
        # Если BTC падает сильно — не открывать LONG на золоте
        if direction == "LONG" and self.btc_momentum < -0.3:
            return False
        # Если BTC растёт сильно — не открывать SHORT
        if direction == "SHORT" and self.btc_momentum > 0.3:
            return False
        return True

    def _auto_optimize(self):
        """Авто-оптимизация SL/TP на основе последних результатов."""
        if len(self.closed_trades) < 10:
            return
        recent = self.closed_trades[-10:]
        wins = [t for t in recent if t.is_win]
        losses = [t for t in recent if not t.is_win]
        win_rate = len(wins) / len(recent)

        # Если много проигрышей — ужесточаем вход
        if win_rate < 0.35:
            old_conf = self.confidence_thr
            self.confidence_thr = min(self.confidence_thr + 5, 90)
            if self.confidence_thr != old_conf:
                self._log_event(clr(f"🔧 Авто-опт: confidence {old_conf:.0f}→{self.confidence_thr:.0f}%", C.YELLOW))

        # Если много побед — можем чуть расслабить
        elif win_rate > 0.65:
            old_conf = self.confidence_thr
            self.confidence_thr = max(self.confidence_thr - 2, 60)
            if self.confidence_thr != old_conf:
                self._log_event(clr(f"🔧 Авто-опт: confidence {old_conf:.0f}→{self.confidence_thr:.0f}%", C.GREEN))

        # Статистика по паттернам
        for trade in recent:
            reason = trade.pos.reason
            if reason not in self._pattern_stats:
                self._pattern_stats[reason] = []
            self._pattern_stats[reason].append(trade.net_pnl)

    # ──────────────────────────────────────────
    # WebSocket: реальные данные MEXC
    # ──────────────────────────────────────────

    async def connect_and_run(self):
        """Подключиться к MEXC и запустить главный цикл.
        При geo-block WS автоматически переключается на REST-поллинг.
        """
        retry_delay = 2.0
        ws_attempts = 0

        while self.running:
            if self.ws_blocked:
                # ── REST-режим (geo-block обнаружен) ──
                await self._rest_poll_loop()
                break

            try:
                await self._ws_session()
                retry_delay = 2.0
                ws_attempts = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.warning(f"WS error: {e}")
                ws_attempts += 1
                self._log_event(clr(f"⚡ WS reconnect in {retry_delay:.0f}s...", C.YELLOW))
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def _ws_session(self):
        """Одна сессия WebSocket. Обнаруживает geo-block и выставляет флаг."""
        try:
            import websockets
        except ImportError:
            print(f"\n{C.RED}ERROR: websockets не установлен.{C.RESET}")
            print("Выполните: pip install websockets aiohttp")
            self.running = False
            return

        depth_ch  = f"spot@public.depth.v3.api@{self.symbol}@{self.depth_level}"
        trades_ch = f"spot@public.deals.v3.api@{self.symbol}"

        async with websockets.connect(
            MEXC_WS_URL,
            ping_interval=None,
            max_size=8 * 1024 * 1024,
            open_timeout=15,
        ) as ws:
            # Подписка
            await ws.send(json.dumps({
                "method": "SUBSCRIPTION",
                "params": [depth_ch, trades_ch],
            }))

            # Читаем первый ответ — проверяем на geo-block
            first_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            first_msg = json.loads(first_raw)
            msg_text = first_msg.get("msg", "")
            if WS_BLOCKED_MSG in msg_text:
                self.ws_blocked = True
                self._log_event(clr(
                    "🌍 WS geo-blocked → переключение на REST-поллинг", C.YELLOW
                ))
                logging.warning(f"WS geo-blocked: {msg_text}")
                return  # выходим — connect_and_run переключит на REST

            self._log_event(clr("✅ Подключено к MEXC WebSocket", C.GREEN))

            # Ping каждые 20с
            ping_task = asyncio.create_task(self._ping_loop(ws))

            # Обработать первое сообщение, потом читать дальше
            await self._handle_message(first_msg)

            async for raw in ws:
                if not self.running:
                    break
                self.ws_messages += 1
                try:
                    msg = json.loads(raw)
                    await self._handle_message(msg)
                except Exception as e:
                    logging.debug(f"msg parse error: {e}")

            ping_task.cancel()

    async def _rest_poll_loop(self):
        """REST-поллинг стакана через отдельный поток."""
        import urllib.request
        import urllib.parse
        import json as _json
        import threading
        import queue

        url = f"{MEXC_REST_URL}/api/v3/depth"
        full_url = url + "?" + urllib.parse.urlencode({"symbol": self.symbol, "limit": self.depth_level})
        poll_interval = 2.0
        result_queue = queue.Queue()

        self._log_event(clr(f"📡 REST-режим: поллинг каждые {poll_interval:.1f}с", C.CYAN))

        def worker():
            i = 0
            while True:
                i += 1
                try:
                    req = urllib.request.Request(full_url, headers={"Connection": "close"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = _json.loads(resp.read().decode())
                    result_queue.put(("ok", data))
                except Exception as e:
                    result_queue.put(("err", str(e)))
                time.sleep(poll_interval)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while self.running:
            try:
                kind, payload = result_queue.get_nowait()
                if kind == "ok":
                    # REST возвращает полный снапшот — сбрасываем стакан перед обработкой
                    self.analyzer._bids.clear()
                    self.analyzer._asks.clear()
                    updated = await self.analyzer.process(payload)
                    if updated:
                        self.rest_polls += 1
                        self.last_trade_price = self.analyzer.mid_price
                        self.price_history.append(self.analyzer.mid_price)
                        await self._on_book_update()
                else:
                    logging.warning(f"REST poll error: {payload}")
                    self._log_event(clr(f"⚠️ {payload}", C.YELLOW))
            except queue.Empty:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.warning(f"REST process error: {e}", exc_info=True)
            await asyncio.sleep(0.2)

    async def _ping_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"method": "PING"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_message(self, msg: dict):
        """Разобрать входящее WebSocket-сообщение."""
        channel = msg.get("c", "")
        data    = msg.get("d", {})

        # Стакан ордеров (реальный)
        if "depth" in channel:
            updated = await self.analyzer.process(data)
            if updated:
                await self._on_book_update()

        # Сделки (тики) — для актуальной цены
        elif "deals" in channel:
            deals = data.get("deals", [data])
            for deal in deals:
                try:
                    p = float(deal.get("p", 0))
                    if p > 0:
                        self.last_trade_price = p
                        self.price_history.append(p)
                        if self.position:
                            await self._check_sl_tp(p)
                except Exception:
                    pass

    async def _on_book_update(self):
        """Реакция на обновление стакана."""
        price = self.analyzer.mid_price

        # Обновить RSI
        self._update_rsi(price)

        # Обновить мультитаймфрейм
        self._update_timeframes(price)

        # Обновить уровни S/R
        self._update_sr_levels()

        # Обновить цену BTC для корреляции
        await self._fetch_btc_price()

        # Ежечасный отчёт
        await self._send_hourly_report()

        # Еженедельный отчёт
        await self._send_weekly_report()

        # Сохранить историю каждые 50 обновлений
        if self.analyzer.update_count % 50 == 0:
            self._save_history()

        # Обновить позицию + тайм-аут
        if self.position:
            await self._check_sl_tp(price)
            await self._check_position_timeout()

        # Генерировать сигнал
        if self.position is None:
            if not self._can_trade():
                return

            # Пауза после 3 SL подряд
            if self.consec_losses >= 3 and self._sl_pause_until == 0:
                self._sl_pause_until = time.time() + 600  # 10 минут
                self._log_event(clr("⏸️ Пауза 10 мин после 3 SL подряд", C.YELLOW))
                await self._tg("⏸️ Пауза 10 минут после 3 SL подряд")
                return
            elif time.time() > self._sl_pause_until and self._sl_pause_until > 0:
                self._sl_pause_until = 0.0
                self.consec_losses = 0
                self._log_event(clr("▶️ Пауза закончена, торговля возобновлена", C.GREEN))

            sig = self.analyzer.generate_signal(
                confidence_threshold=self.confidence_thr,
                sl_pct=self.sl_pct,
                tp_pct=self.tp_pct,
            )
            if sig:
                # RSI фильтр
                if not self._is_rsi_ok(sig["direction"]):
                    return
                # BTC корреляция
                if not self._is_btc_corr_ok(sig["direction"]):
                    return
                # Мультитаймфрейм
                if not self._is_tf_ok(sig["direction"]):
                    return
                # Уровни S/R
                if not self._is_sr_ok(sig["direction"], sig["entry"]):
                    return
                self.signals_generated += 1
                self.last_signal = sig
                await self._open_position(sig)

    async def _check_sl_tp(self, price: float):
        """Проверить срабатывание SL/TP + трейлинг стоп."""
        if self.position is None:
            return

        # ── ТРЕЙЛИНГ СТОП ──
        # Если позиция в плюсе больше 0.1% — подтягиваем SL
        pos = self.position
        if pos.direction == "LONG" and price > pos.entry_price:
            profit_pct = (price - pos.entry_price) / pos.entry_price * 100
            if profit_pct > 0.1:
                # Новый SL = текущая цена - половина SL дистанции
                sl_dist = pos.entry_price - pos.stop_loss
                new_sl = price - sl_dist * 0.7
                if new_sl > pos.stop_loss:
                    pos.stop_loss = new_sl
        elif pos.direction == "SHORT" and price < pos.entry_price:
            profit_pct = (pos.entry_price - price) / pos.entry_price * 100
            if profit_pct > 0.1:
                sl_dist = pos.stop_loss - pos.entry_price
                new_sl = price + sl_dist * 0.7
                if new_sl < pos.stop_loss:
                    pos.stop_loss = new_sl

        result = pos.update_price(price)
        if result:
            exit_price = (
                pos.stop_loss   if result == "SL" else
                pos.take_profit if result == "TP" else price
            )
            await self._close_position(result, exit_price)

    # ──────────────────────────────────────────
    # Управление виртуальными позициями
    # ──────────────────────────────────────────

    async def _open_position(self, sig: dict):
        """Открыть виртуальную позицию. Сначала спрашивает DeepSeek."""
        # ── Фильтр DeepSeek ──
        ai_ok = await self._ask_deepseek(sig)
        if not ai_ok:
            self._log_event(clr(f"🤖 DeepSeek отклонил {sig['direction']} @ {sig['entry']:.2f}", C.YELLOW))
            return

        # Динамический размер позиции
        dynamic_risk = self._get_dynamic_risk()
        risk_amount = self.balance * dynamic_risk / 100
        quantity    = risk_amount / sig["entry"]
        if quantity <= 0:
            return

        self._daily_trade_count += 1

        self.position = VirtualPosition(
            symbol      = self.symbol,
            direction   = sig["direction"],
            entry_price = sig["entry"],
            quantity    = quantity,
            stop_loss   = sig["sl"],
            take_profit = sig["tp"],
            reason      = sig["reason"],
            confidence  = sig["confidence"],
        )

        emoji = "🟢 LONG" if sig["direction"] == "LONG" else "🔴 SHORT"
        self._log_event(
            f"{clr(emoji, C.GREEN if 'LONG' in emoji else C.RED)} "
            f"@ {sig['entry']:.4f} | "
            f"{clr(sig['reason'], C.CYAN)} "
            f"conf={sig['confidence']:.0f}% RR={sig['rr']}"
        )
        await self._tg(
            f"{emoji} ОТКРЫТА ✅ AI одобрил\n"
            f"Символ: {self.symbol}\n"
            f"Цена входа: {sig['entry']:.4f}\n"
            f"SL: {sig['sl']:.4f} | TP: {sig['tp']:.4f}\n"
            f"Причина: {sig['reason']}\n"
            f"Уверенность: {sig['confidence']:.0f}% | RR: {sig['rr']}"
        )

    async def _ask_deepseek(self, sig: dict) -> bool:
        """Спросить DeepSeek стоит ли входить в сделку. Возвращает True/False."""
        import urllib.request
        import urllib.parse
        import json as _json

        # Считаем bid/ask imbalance в процентах
        total_vol = self.analyzer.total_bid_vol + self.analyzer.total_ask_vol
        bid_imb_pct = self.analyzer.total_bid_vol / total_vol * 100 if total_vol > 0 else 50
        ask_imb_pct = self.analyzer.total_ask_vol / total_vol * 100 if total_vol > 0 else 50

        system_prompt = """Ты — профессиональный алгоритмический скальпер для XAUT/USDT на MEXC (фьючерсы, без комиссии).
Цель: фильтровать шум и пытаться извлечь ~0.5$ прибыли с депозита 10$ за день, при этом сохранять депозит.

LONG — открывать только если ВСЕ условия:
1. Bid imbalance ≥ 70%
2. Delta > avg_delta * 1.7
3. Momentum ≥ 0.05%
4. Нет крупной sell-стены ±0.15%
5. Spread узкий
6. Волатильность выше среднего
7. Сигнал держится ≥3 сек
8. Confidence ≥85%
9. Нет дублирующей открытой позиции
10. Не входить сразу после серии SL

SHORT — открывать только если ВСЕ условия:
1. Ask imbalance ≥ 70%
2. Delta < avg_delta * -1.7
3. Momentum ≤ -0.05%
4. Нет крупной buy-стены ±0.15%
5. Spread узкий
6. Волатильность выше среднего
7. Сигнал держится ≥3 сек
8. Confidence ≥85%
9. Нет дублирующей открытой позиции
10. Не входить сразу после серии SL

ФИЛЬТРЫ:
- Не торговать если momentum < 0.03% (флэт)
- После SL пауза 30-60 сек
- После 3 SL подряд — пауза 5 мин
- Игнорировать >80% сигналов, только сильные импульсы
- Максимум 10-12 сделок в день

РИСК: TP 0.5-0.8%, SL 0.25-0.4%, RR ≥ 1:2

Ответь СТРОГО в JSON:
{"decision": "LONG/SHORT/SKIP", "confidence": 0-100, "reason": "...", "market_state": "impulse/trend/chop", "signal_strength": "weak/medium/strong"}

ФИНАЛЬНОЕ ПРАВИЛО: пропускай большинство сигналов, входи только в сильные импульсы."""

        # История последних 5 сделок для обучения AI
        trade_history = ""
        if self.closed_trades:
            trade_history = "\n--- ИСТОРИЯ ПОСЛЕДНИХ СДЕЛОК ---\n"
            for t in self.closed_trades[-5:]:
                result = "TP ✅" if t.is_win else "SL ❌"
                trade_history += (
                    f"{t.pos.direction} @ {t.pos.entry_price:.2f} → {result} "
                    f"PnL: {t.net_pnl:+.4f} причина: {t.pos.reason}\n"
                )

        user_data = (
            f"Символ: {self.symbol}\n"
            f"Предложенное направление: {sig['direction']}\n"
            f"Цена: {sig['entry']:.4f}\n"
            f"Причина стратегии: {sig['reason']}\n"
            f"Уверенность стратегии: {sig['confidence']:.0f}%\n"
            f"Signal Score: {self.analyzer.signal_score:.1f}/100\n"
            f"--- СТАКАН ---\n"
            f"Bid imbalance: {bid_imb_pct:.1f}%\n"
            f"Ask imbalance: {ask_imb_pct:.1f}%\n"
            f"Bid/Ask ratio: {self.analyzer.imbalance:.3f}\n"
            f"Спред: {self.analyzer.spread_pct:.4f}%\n"
            f"Тающих bid стен: {len(self.analyzer.melting_bid)}\n"
            f"Тающих ask стен: {len(self.analyzer.melting_ask)}\n"
            f"--- DELTA ---\n"
            f"Delta 1s: {self.analyzer.delta_1s:.4f}\n"
            f"Delta 3s: {self.analyzer.delta_3s:.4f}\n"
            f"Delta 5s: {self.analyzer.delta_5s:.4f}\n"
            f"Avg delta: {self.analyzer.avg_delta:.4f}\n"
            f"--- MOMENTUM ---\n"
            f"Momentum 1s: {self.analyzer.momentum_1s:.4f}%\n"
            f"Momentum 3s: {self.analyzer.momentum_3s:.4f}%\n"
            f"Momentum 5s: {self.analyzer.momentum_5s:.4f}%\n"
            f"--- РЫНОК ---\n"
            f"Market direction: {self.analyzer.market_direction}\n"
            f"Market strength: {self.analyzer.market_strength:.1f}\n"
            f"Is flat: {self.analyzer.is_flat}\n"
            f"Volume spike: {self.analyzer.volume_spike} ({self.analyzer.volume_spike_dir})\n"
            f"Spoofing detected: {self.analyzer.spoofing_detected}\n"
            f"{trade_history}"
            f"Стоит ли входить? Ответь JSON."
        )

        try:
            url = "https://api.deepseek.com/chat/completions"
            payload = _json.dumps({
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_data},
                ],
                "max_tokens": 100,
                "temperature": 0.0,
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer sk-e6e8605f0ec449f9b79cacdd1a4c96a3",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
                answer = data["choices"][0]["message"]["content"].strip()
                logging.info(f"DeepSeek: {answer}")
                # Парсим JSON ответ
                try:
                    # Убираем markdown если есть
                    clean = answer.replace("```json", "").replace("```", "").strip()
                    result = _json.loads(clean)
                    decision = result.get("decision", "SKIP").upper()
                    confidence = result.get("confidence", 0)
                    strength = result.get("signal_strength", "weak")
                    market = result.get("market_state", "chop")
                    logging.info(f"DeepSeek решение: {decision} conf={confidence} {strength} {market}")
                    # Входим только если AI согласен с направлением И сигнал сильный
                    return (decision == sig["direction"] and
                            confidence >= 70 and
                            strength in ("medium", "strong") and
                            market != "chop")
                except Exception:
                    # Если JSON не распарсился — смотрим на текст
                    return "SKIP" not in answer.upper() and sig["direction"] in answer.upper()
        except Exception as e:
            logging.warning(f"DeepSeek error: {e} — пропускаем фильтр")
            return True  # при ошибке API — входим как обычно

    async def _tg(self, text: str):
        """Отправить уведомление в Telegram."""
        import urllib.request
        import urllib.parse
        token = "8391650748:AAF85_FqPdC3hoOXAGymCh5krfilRcSqd1s"
        chat_id = "5259105676"
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            }).encode()
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logging.warning(f"TG error: {e}")

    async def _tg_poll_commands(self):
        """Читать команды из Telegram и выполнять их."""
        import urllib.request
        import urllib.parse
        import json as _j
        token = "8391650748:AAF85_FqPdC3hoOXAGymCh5krfilRcSqd1s"
        chat_id = "5259105676"

        try:
            # Получаем последние апдейты
            offset = getattr(self, '_tg_offset', 0)
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=1"
            loop = asyncio.get_event_loop()
            def fetch():
                with urllib.request.urlopen(url, timeout=5) as r:
                    return _j.loads(r.read())
            data = await loop.run_in_executor(None, fetch)

            for update in data.get("result", []):
                self._tg_offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                from_id = str(msg.get("chat", {}).get("id", ""))

                # Только от нашего чата
                if from_id != chat_id:
                    continue

                if text == "/status":
                    n = len(self.closed_trades)
                    wr = self.win_rate
                    pnl = self.total_pnl
                    sign = "+" if pnl >= 0 else ""
                    pos_str = f"Позиция: {self.position.direction} @ {self.position.entry_price:.4f}" if self.position else "Позиция: нет"
                    await self._tg(
                        f"📊 <b>СТАТУС БОТА</b>\n"
                        f"Символ: {self.symbol}\n"
                        f"Баланс: {self.balance:.4f} USDT\n"
                        f"PnL: {sign}{pnl:.4f} USDT\n"
                        f"Сделок: {n} | WinRate: {wr:.1f}%\n"
                        f"{pos_str}\n"
                        f"RSI: {self.rsi:.1f}\n"
                        f"BTC: {self._btc_direction}\n"
                        f"Confidence: {self.confidence_thr:.0f}%\n"
                        f"Дневных сделок: {self._daily_trade_count}/{self._daily_trade_limit}"
                    )

                elif text == "/balance":
                    pnl = self.total_pnl
                    sign = "+" if pnl >= 0 else ""
                    await self._tg(
                        f"💰 Баланс: <b>{self.balance:.4f} USDT</b>\n"
                        f"Старт: {self.start_balance:.4f} USDT\n"
                        f"PnL: {sign}{pnl:.4f} USDT ({pnl/self.start_balance*100:+.2f}%)"
                    )

                elif text == "/stop":
                    await self._tg("⛔ Бот остановлен по команде /stop")
                    self.running = False

                elif text == "/pause":
                    self._sl_pause_until = time.time() + 3600  # пауза 1 час
                    await self._tg("⏸️ Торговля приостановлена на 1 час. /resume для возобновления")

                elif text == "/resume":
                    self._sl_pause_until = 0.0
                    self.consec_losses = 0
                    await self._tg("▶️ Торговля возобновлена!")

                elif text == "/trades":
                    if not self.closed_trades:
                        await self._tg("Сделок пока нет")
                    else:
                        lines = ["📋 <b>Последние 5 сделок:</b>"]
                        for t in self.closed_trades[-5:][::-1]:
                            sign = "+" if t.net_pnl >= 0 else ""
                            icon = "✅" if t.is_win else "❌"
                            lines.append(
                                f"{icon} {t.pos.direction} {t.pos.entry_price:.2f}→{t.exit_price:.2f} "
                                f"{sign}{t.net_pnl:.4f} USDT"
                            )
                        await self._tg("\n".join(lines))

                elif text == "/help":
                    await self._tg(
                        "🤖 <b>Команды бота:</b>\n"
                        "/status — статус и статистика\n"
                        "/balance — баланс и PnL\n"
                        "/trades — последние 5 сделок\n"
                        "/pause — пауза на 1 час\n"
                        "/resume — возобновить торговлю\n"
                        "/stop — остановить бота\n"
                        "/help — эта справка"
                    )

        except Exception as e:
            logging.debug(f"TG poll error: {e}")

    async def _close_position(self, reason: str, exit_price: float):
        """Закрыть виртуальную позицию."""
        if self.position is None:
            return
        trade = ClosedTrade(self.position, exit_price, reason, self.commission_rate)
        self.closed_trades.append(trade)
        self.balance   += trade.net_pnl
        self.daily_pnl += trade.net_pnl

        if trade.is_win:
            self.consec_losses = 0
            emoji = "✅"
            col = C.GREEN
        else:
            self.consec_losses += 1
            emoji = "❌"
            col = C.RED

        sign = "+" if trade.net_pnl >= 0 else ""
        self._log_event(
            f"{emoji} {reason} @ {exit_price:.4f} | "
            f"PnL: {clr(f'{sign}{trade.net_pnl:.4f} USDT ({sign}{trade.pnl_pct:.2f}%)', col)} | "
            f"Dur: {trade.duration:.0f}s"
        )
        await self._tg(
            f"{emoji} ЗАКРЫТА {reason}\n"
            f"Символ: {self.symbol}\n"
            f"Вход: {trade.pos.entry_price:.4f} → Выход: {exit_price:.4f}\n"
            f"PnL: {sign}{trade.net_pnl:.4f} USDT ({sign}{trade.pnl_pct:.2f}%)\n"
            f"RSI: {self.rsi:.1f} | Score: {self.analyzer.signal_score:.0f}\n"
            f"Длительность: {trade.duration:.0f}с\n"
            f"Баланс: {self.balance:.4f} USDT"
        )
        self.position = None

        # Сохранить в CSV
        self._save_trade_csv(trade)

        # Статистика по часам
        hour = datetime.now().hour
        if hour not in self._hourly_stats:
            self._hourly_stats[hour] = {"wins": 0, "losses": 0, "pnl": 0}
        if trade.is_win:
            self._hourly_stats[hour]["wins"] += 1
        else:
            self._hourly_stats[hour]["losses"] += 1
        self._hourly_stats[hour]["pnl"] += trade.net_pnl

        # Авто-оптимизация каждые 10 сделок
        self._auto_opt_counter += 1
        if self._auto_opt_counter % 10 == 0:
            self._auto_optimize()

    def _save_trade_csv(self, trade: "ClosedTrade"):
        """Сохранить сделку в CSV файл."""
        import csv
        os.makedirs("logs", exist_ok=True)
        fname = "logs/trades.csv"
        file_exists = os.path.exists(fname)
        try:
            with open(fname, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["time", "symbol", "direction", "entry", "exit",
                                     "reason", "pnl", "pnl_pct", "duration", "result",
                                     "rsi", "score", "btc_dir"])
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    self.symbol,
                    trade.pos.direction,
                    f"{trade.pos.entry_price:.4f}",
                    f"{trade.exit_price:.4f}",
                    trade.pos.reason,
                    f"{trade.net_pnl:.4f}",
                    f"{trade.pnl_pct:.2f}",
                    f"{trade.duration:.0f}",
                    "WIN" if trade.is_win else "LOSS",
                    f"{self.rsi:.1f}",
                    f"{self.analyzer.signal_score:.0f}",
                    self._btc_direction,
                ])
        except Exception as e:
            logging.warning(f"CSV save error: {e}")

    def _can_trade(self) -> bool:
        """Проверить все лимиты риска."""
        # Сбросить дневной счётчик при новом дне
        today = datetime.now().day
        if today != self._last_day:
            self._daily_trade_count = 0
            self.daily_pnl = 0.0
            self._last_day = today

        # Фильтр времени — не торговать ночью 01:00-06:00 UTC
        utc_hour = datetime.utcnow().hour
        if 1 <= utc_hour < 6:
            return False

        # Дневной лимит сделок
        if self._daily_trade_count >= self._daily_trade_limit:
            return False

        # Пауза после серии SL
        if time.time() < self._sl_pause_until:
            return False

        if self.consec_losses >= self.max_consec:
            return False
        if self.start_balance > 0:
            loss_pct = -self.daily_pnl / self.start_balance * 100
            if loss_pct >= self.max_daily_loss:
                return False
        return True

    async def _check_position_timeout(self):
        """Закрыть позицию если висит слишком долго без движения."""
        if self.position is None:
            return
        pos = self.position
        age = pos.age
        # Если позиция висит >8 минут и PnL близко к 0 — закрываем
        if age > 480:
            pnl_pct = abs(pos.pnl_pct)
            if pnl_pct < 0.05:
                self._log_event(clr(f"⏰ Тайм-аут позиции ({age:.0f}с без движения)", C.YELLOW))
                await self._close_position("TIMEOUT", self.analyzer.mid_price)

    async def _send_hourly_report(self):
        """Отправить ежечасный отчёт в Telegram."""
        if time.time() - self._last_hourly_report < 3600:
            return
        self._last_hourly_report = time.time()

        n = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.is_win)
        wr = wins / n * 100 if n > 0 else 0
        pnl = self.total_pnl

        # Лучший час
        best_hour = ""
        if self._hourly_stats:
            best = max(self._hourly_stats.items(), key=lambda x: x[1]["pnl"])
            best_hour = f"\n⭐ Лучший час: {best[0]:02d}:00 (+{best[1]['pnl']:.4f})"

        sign = "+" if pnl >= 0 else ""
        await self._tg(
            f"📊 ЕЖЕЧАСНЫЙ ОТЧЁТ\n"
            f"Символ: {self.symbol}\n"
            f"Сделок: {n} | WinRate: {wr:.1f}%\n"
            f"PnL: {sign}{pnl:.4f} USDT\n"
            f"Баланс: {self.balance:.4f} USDT\n"
            f"Confidence: {self.confidence_thr:.0f}%\n"
            f"Дневных сделок: {self._daily_trade_count}/{self._daily_trade_limit}"
            f"{best_hour}"
        )

    def _get_best_hours(self) -> str:
        """Получить топ-3 лучших часа для торговли."""
        if not self._hourly_stats:
            return "нет данных"
        sorted_hours = sorted(self._hourly_stats.items(),
                              key=lambda x: x[1]["pnl"], reverse=True)
        return ", ".join(f"{h:02d}:00" for h, _ in sorted_hours[:3])

    # ──────────────────────────────────────────
    # УРОВНИ ПОДДЕРЖКИ / СОПРОТИВЛЕНИЯ
    # ──────────────────────────────────────────

    def _update_sr_levels(self):
        """Рассчитать уровни поддержки и сопротивления из истории цен."""
        if time.time() - self._sr_last_update < 30:
            return
        self._sr_last_update = time.time()
        prices = list(self.price_history)
        if len(prices) < 20:
            return
        # Находим локальные максимумы и минимумы
        supports, resistances = [], []
        for i in range(2, len(prices) - 2):
            # Локальный минимум — поддержка
            if prices[i] < prices[i-1] and prices[i] < prices[i-2] and \
               prices[i] < prices[i+1] and prices[i] < prices[i+2]:
                supports.append(prices[i])
            # Локальный максимум — сопротивление
            if prices[i] > prices[i-1] and prices[i] > prices[i-2] and \
               prices[i] > prices[i+1] and prices[i] > prices[i+2]:
                resistances.append(prices[i])
        self._support_levels = sorted(set(round(p, 1) for p in supports))[-3:]
        self._resistance_levels = sorted(set(round(p, 1) for p in resistances))[:3]

    def _is_sr_ok(self, direction: str, price: float) -> bool:
        """Проверить что не входим прямо в уровень."""
        threshold = price * 0.001  # 0.1%
        if direction == "LONG":
            # Не входить если сразу выше сопротивление
            for r in self._resistance_levels:
                if abs(r - price) < threshold:
                    self._log_event(clr(f"🚫 Сопротивление @ {r:.2f}", C.YELLOW))
                    return False
        elif direction == "SHORT":
            # Не входить если сразу ниже поддержка
            for s in self._support_levels:
                if abs(s - price) < threshold:
                    self._log_event(clr(f"🚫 Поддержка @ {s:.2f}", C.YELLOW))
                    return False
        return True

    # ──────────────────────────────────────────
    # МУЛЬТИТАЙМФРЕЙМ
    # ──────────────────────────────────────────

    def _update_timeframes(self, price: float):
        """Обновить данные мультитаймфрейма."""
        now = time.time()
        if now - self._tf_last_1m >= 1:
            self._tf_1m.append(price)
            self._tf_last_1m = now
        if now - self._tf_last_5m >= 5:
            self._tf_5m.append(price)
            self._tf_last_5m = now
        if now - self._tf_last_15m >= 15:
            self._tf_15m.append(price)
            self._tf_last_15m = now
        self._calc_tf_agreement()

    def _calc_tf_agreement(self):
        """Определить направление на всех таймфреймах."""
        def trend(prices):
            if len(prices) < 3:
                return "flat"
            p = list(prices)
            change = (p[-1] - p[0]) / p[0] * 100 if p[0] > 0 else 0
            if change > 0.03:
                return "up"
            elif change < -0.03:
                return "down"
            return "flat"

        t1 = trend(self._tf_1m)
        t5 = trend(self._tf_5m)
        t15 = trend(self._tf_15m)

        # Согласие когда хотя бы 2 из 3 согласны
        votes_up = [t for t in [t1, t5, t15] if t == "up"]
        votes_down = [t for t in [t1, t5, t15] if t == "down"]

        if len(votes_up) >= 2:
            self.tf_agreement = "long"
        elif len(votes_down) >= 2:
            self.tf_agreement = "short"
        else:
            self.tf_agreement = "none"

    def _is_tf_ok(self, direction: str) -> bool:
        """Проверить согласие таймфреймов."""
        if self.tf_agreement == "none":
            return True  # нет данных — не блокируем
        if direction == "LONG" and self.tf_agreement == "short":
            self._log_event(clr("🚫 MTF: таймфреймы против LONG", C.YELLOW))
            return False
        if direction == "SHORT" and self.tf_agreement == "long":
            self._log_event(clr("🚫 MTF: таймфреймы против SHORT", C.YELLOW))
            return False
        return True

    # ──────────────────────────────────────────
    # ДИНАМИЧЕСКИЙ РАЗМЕР ПОЗИЦИИ
    # ──────────────────────────────────────────

    def _get_dynamic_risk(self) -> float:
        """Рассчитать размер позиции динамически."""
        base = self._base_risk_pct
        # После 2+ побед подряд — увеличиваем на 20%
        if self._win_streak >= 2:
            multiplier = min(1.0 + self._win_streak * 0.1, 1.5)
        # После 2+ убытков подряд — уменьшаем на 30%
        elif self._loss_streak >= 2:
            multiplier = max(1.0 - self._loss_streak * 0.15, 0.5)
        else:
            multiplier = 1.0
        self._streak_multiplier = multiplier
        return base * multiplier

    # ──────────────────────────────────────────
    # ПАМЯТЬ СДЕЛОК МЕЖДУ СЕССИЯМИ
    # ──────────────────────────────────────────

    def _save_history(self):
        """Сохранить историю сделок в JSON."""
        import json as _j
        try:
            os.makedirs("logs", exist_ok=True)
            data = {
                "balance": self.balance,
                "total_trades": len(self.closed_trades),
                "wins": sum(1 for t in self.closed_trades if t.is_win),
                "total_pnl": self.total_pnl,
                "hourly_stats": self._hourly_stats,
                "confidence_thr": self.confidence_thr,
                "sl_pct": self.sl_pct,
                "tp_pct": self.tp_pct,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(self._history_file, "w") as f:
                _j.dump(data, f, indent=2)
        except Exception as e:
            logging.warning(f"History save error: {e}")

    def _load_history(self):
        """Загрузить историю из прошлой сессии."""
        import json as _j
        try:
            if not os.path.exists(self._history_file):
                return
            with open(self._history_file) as f:
                data = _j.load(f)
            # Восстанавливаем параметры
            self._hourly_stats = {int(k): v for k, v in data.get("hourly_stats", {}).items()}
            self.confidence_thr = data.get("confidence_thr", self.confidence_thr)
            prev_trades = data.get("total_trades", 0)
            prev_pnl = data.get("total_pnl", 0)
            self._log_event(clr(f"📂 Загружена история: {prev_trades} сделок, PnL: {prev_pnl:+.4f}", C.CYAN))
            logging.info(f"History loaded: {prev_trades} trades, pnl={prev_pnl}")
        except Exception as e:
            logging.warning(f"History load error: {e}")

    # ──────────────────────────────────────────
    # ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ
    # ──────────────────────────────────────────

    async def _send_weekly_report(self):
        """Отправить еженедельный отчёт каждое воскресенье."""
        now = datetime.now()
        if time.time() - self._last_weekly_report < 604800:  # раз в 7 дней
            return
        # Или если воскресенье 23:00
        if not (now.weekday() == 6 and now.hour == 23):
            return
        self._last_weekly_report = time.time()

        n = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.is_win)
        wr = wins / n * 100 if n > 0 else 0
        pnl = self.total_pnl
        sign = "+" if pnl >= 0 else ""

        # Лучший и худший день
        best_h = self._get_best_hours()

        await self._tg(
            f"📅 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ\n"
            f"{'='*25}\n"
            f"Символ: {self.symbol}\n"
            f"Всего сделок: {n}\n"
            f"Победы: {wins} | Поражения: {n-wins}\n"
            f"WinRate: {wr:.1f}%\n"
            f"Итоговый PnL: {sign}{pnl:.4f} USDT\n"
            f"Баланс: {self.balance:.4f} USDT\n"
            f"⭐ Лучшие часы: {best_h}\n"
            f"Confidence: {self.confidence_thr:.0f}%\n"
            f"SL: {self.sl_pct:.2f}% | TP: {self.tp_pct:.2f}%"
        )

    def _update_rsi(self, price: float):
        """Рассчитать RSI-14."""
        self._rsi_prices.append(price)
        if len(self._rsi_prices) < 2:
            return
        prices = list(self._rsi_prices)
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i-1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        if not gains:
            return
        avg_gain = sum(gains) / len(gains)
        avg_loss = sum(losses) / len(losses)
        if avg_loss == 0:
            self.rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            self.rsi = 100 - (100 / (1 + rs))

    def _is_rsi_ok(self, direction: str) -> bool:
        """Проверить RSI фильтр."""
        if direction == "LONG" and self.rsi > 75:
            self._log_event(clr(f"🚫 RSI={self.rsi:.0f} — перекуплен, LONG заблокирован", C.YELLOW))
            return False
        if direction == "SHORT" and self.rsi < 25:
            self._log_event(clr(f"🚫 RSI={self.rsi:.0f} — перепродан, SHORT заблокирован", C.YELLOW))
            return False
        return True

    async def _fetch_btc_price(self):
        """Получить цену BTC для корреляции (раз в 5 сек)."""
        if time.time() - self._last_btc_update < 5:
            return
        if self.symbol == "BTCUSDT":
            return  # не нужно если торгуем BTC
        import urllib.request, json as _j
        try:
            loop = asyncio.get_event_loop()
            def get_btc():
                url = "https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT"
                with urllib.request.urlopen(url, timeout=3) as r:
                    return _j.loads(r.read())
            data = await loop.run_in_executor(None, get_btc)
            btc_price = float(data.get("price", 0))
            if btc_price > 0:
                self._btc_price = btc_price
                self._btc_history.append(btc_price)
                self._last_btc_update = time.time()
                # Определяем направление BTC
                if len(self._btc_history) >= 3:
                    prices = list(self._btc_history)
                    change = (prices[-1] - prices[-3]) / prices[-3] * 100
                    if change > 0.05:
                        self._btc_direction = "up"
                    elif change < -0.05:
                        self._btc_direction = "down"
                    else:
                        self._btc_direction = "flat"
        except Exception:
            pass

    def _is_btc_corr_ok(self, direction: str) -> bool:
        """Проверить корреляцию с BTC."""
        if self._btc_price == 0:
            return True  # нет данных — не блокируем
        if self._btc_direction == "down" and direction == "LONG":
            self._log_event(clr(f"🚫 BTC↓ ({self._btc_price:.0f}) — LONG заблокирован", C.YELLOW))
            return False
        if self._btc_direction == "up" and direction == "SHORT":
            self._log_event(clr(f"🚫 BTC↑ ({self._btc_price:.0f}) — SHORT заблокирован", C.YELLOW))
            return False
        return True

    async def _update_btc_correlation(self):
        """Обёртка для обновления BTC."""
        await self._fetch_btc_price()

    def _auto_optimize(self):
        """Авто-оптимизация SL/TP на основе статистики."""
        if len(self.closed_trades) < 10:
            return

        # Считаем статистику по причинам сигналов
        stats: Dict[str, dict] = {}
        for t in self.closed_trades[-20:]:
            r = t.pos.reason
            if r not in stats:
                stats[r] = {"wins": 0, "losses": 0, "pnl": 0}
            if t.is_win:
                stats[r]["wins"] += 1
            else:
                stats[r]["losses"] += 1
            stats[r]["pnl"] += t.net_pnl

        self._best_reason_stats = stats

        # Если много SL — увеличиваем уверенность
        recent = self.closed_trades[-10:]
        losses = sum(1 for t in recent if not t.is_win)
        if losses >= 7:
            old = self.confidence_thr
            self.confidence_thr = min(self.confidence_thr + 5, 90)
            if self.confidence_thr != old:
                self._log_event(clr(f"⚙️ Авто-опт: confidence {old:.0f}% → {self.confidence_thr:.0f}%", C.CYAN))

        # Если много TP — немного расслабляем
        elif losses <= 2:
            old = self.confidence_thr
            self.confidence_thr = max(self.confidence_thr - 2, 60)
            if self.confidence_thr != old:
                self._log_event(clr(f"⚙️ Авто-опт: confidence {old:.0f}% → {self.confidence_thr:.0f}%", C.CYAN))

        # Адаптируем SL/TP
        avg_win = sum(t.net_pnl for t in recent if t.is_win) / max(1, sum(1 for t in recent if t.is_win))
        avg_loss = abs(sum(t.net_pnl for t in recent if not t.is_win)) / max(1, losses)
        if avg_loss > 0 and avg_win / avg_loss < 1.5:
            # RR слишком маленький — увеличиваем TP
            old_tp = self.tp_pct
            self.tp_pct = min(self.tp_pct * 1.1, 1.0)
            if abs(self.tp_pct - old_tp) > 0.01:
                self._log_event(clr(f"⚙️ Авто-опт: TP {old_tp:.2f}% → {self.tp_pct:.2f}%", C.CYAN))

    # ──────────────────────────────────────────
    # Статистика
    # ──────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t.is_win)
        return wins / len(self.closed_trades) * 100

    @property
    def total_pnl(self) -> float:
        return self.balance - self.start_balance

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t.net_pnl for t in self.closed_trades if t.net_pnl > 0)
        gross_loss = abs(sum(t.net_pnl for t in self.closed_trades if t.net_pnl <= 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        if not self.closed_trades:
            return 0.0
        cumulative = self.start_balance
        peak = self.start_balance
        max_dd = 0.0
        for t in self.closed_trades:
            cumulative += t.net_pnl
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        return max_dd

    def _mini_chart(self, prices: list, width: int = 32) -> str:
        """Мини-график цены из последних значений."""
        if len(prices) < 2:
            return " " * width
        vals = list(prices)[-width:]
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return "─" * len(vals)
        bars = "▁▂▃▄▅▆▇█"
        chart = ""
        for v in vals:
            idx = int((v - mn) / (mx - mn) * (len(bars) - 1))
            chart += bars[idx]
        return chart

    # ──────────────────────────────────────────
    # Дашборд
    # ──────────────────────────────────────────

    def render_dashboard(self):
        """Отрисовать дашборд в терминале."""
        # Очистить экран
        print("\033[H\033[J", end="")

        W = 70  # ширина

        def line(content="", fill=" "):
            return f"│ {content:<{W-4}} │"

        def sep(ch="─"):
            return f"├{'─'*W}┤" if ch == "─" else f"├{'═'*W}┤"

        def header(title: str):
            pad = (W - len(title) - 2) // 2
            return f"│{' '*pad} {clr(title, C.BOLD+C.CYAN)} {' '*(W-pad-len(title)-2)}│"

        top = f"╔{'═'*W}╗"
        bot = f"╚{'═'*W}╝"
        slt = f"┌{'─'*W}┐"
        slb = f"└{'─'*W}┘"
        mid = f"├{'─'*W}┤"

        uptime = time.time() - self.start_time
        h, rem = divmod(int(uptime), 3600)
        m, s   = divmod(rem, 60)

        now_str = datetime.now().strftime("%H:%M:%S")
        pnl_total = self.total_pnl
        pnl_col = pnl_color(pnl_total)

        if self.ws_blocked:
            mode_str = clr(f"REST-поллинг  •  запросов: {self.rest_polls}", C.YELLOW)
        else:
            mode_str = clr(f"WebSocket  •  msg: {self.ws_messages}", C.GREEN)

        # ─── ШАПКА ───
        print(f"╔{'═'*W}╗")
        title = f"  🤖 CONFI Scalper — ДЕМО-РЕЖИМ   {self.symbol}  "
        print(f"║{clr(title.center(W), C.BOLD+C.WHITE)}║")
        print(f"║{clr(f'  Реальные данные MEXC  •  {mode_str}  •  {now_str}  '.center(W), C.DIM)}║")
        print(f"╚{'═'*W}╝")
        print()

        # ─── БАЛАНС ───
        print(f"┌{'─'*W}┐")
        bal_str = f"{self.balance:>12.4f} USDT"
        pnl_str = f"{'+'if pnl_total>=0 else ''}{pnl_total:.4f} USDT  ({pnl_total/self.start_balance*100:+.2f}%)"
        print(f"│  💰 Баланс: {clr(bal_str, C.BOLD+C.WHITE)}   Старт: {self.start_balance:.2f} USDT{' '*(W-60)}│")
        print(f"│  📊 PnL:    {clr(pnl_str, C.BOLD+pnl_col)}{' '*(W-len(pnl_str)-14)}│")
        # RSI и BTC корреляция
        rsi_col = C.RED if self.rsi > 70 else (C.GREEN if self.rsi < 30 else C.WHITE)
        btc_col = C.GREEN if self._btc_direction == "up" else (C.RED if self._btc_direction == "down" else C.DIM)
        btc_str = f"BTC: {clr(f'{self._btc_price:.0f} ({self._btc_direction})', btc_col)}" if self._btc_price > 0 else ""
        conf_str = f"Conf: {clr(f'{self.confidence_thr:.0f}%', C.CYAN)}"
        print(f"│  RSI: {clr(f'{self.rsi:.1f}', rsi_col)}  {btc_str}  {conf_str}{' '*(W-60)}│")
        print(f"└{'─'*W}┘")
        print()

        # ─── РЫНОК ───
        print(f"┌{'─'*W}┐")
        mid_p = self.analyzer.mid_price
        last_p = self.last_trade_price
        bid = self.analyzer.best_bid
        ask = self.analyzer.best_ask
        spread = self.analyzer.spread_pct
        imb = self.analyzer.imbalance
        imb_col = C.GREEN if imb > 1.3 else (C.RED if imb < 0.77 else C.WHITE)
        chart = self._mini_chart(list(self.price_history))
        data_src = f"REST polls: {self.rest_polls}" if self.ws_blocked else f"WS msgs: {self.ws_messages}"
        print(f"│  📈 {clr(self.symbol, C.BOLD)}  Mid: {clr(f'{mid_p:.4f}', C.BOLD+C.WHITE)}  Last: {last_p:.4f}  Spread: {spread:.3f}%  {data_src}{' '*(W-80)}│")
        print(f"│  Bid: {clr(f'{bid:.4f}', C.GREEN)}  Ask: {clr(f'{ask:.4f}', C.RED)}  Imbalance: {clr(f'{imb:.3f}', imb_col)}  {clr(chart, C.CYAN)}{' '*(W-len(chart)-60)}│")

        # DELTA + MOMENTUM
        mom = self.analyzer.momentum_3s
        mom_col = C.GREEN if mom > 0.02 else (C.RED if mom < -0.02 else C.WHITE)
        delta = self.analyzer.delta_3s
        d_col = C.GREEN if delta > 0 else C.RED
        mdir = self.analyzer.market_direction
        mdir_col = C.GREEN if mdir == "up" else (C.RED if mdir == "down" else C.DIM)
        score = self.analyzer.signal_score
        score_col = C.GREEN if score > 60 else (C.YELLOW if score > 30 else C.DIM)
        flat_str = clr("⚠️FLAT", C.YELLOW) if self.analyzer.is_flat else ""
        spoof_str = clr("⚠️SPOOF", C.RED) if self.analyzer.spoofing_detected else ""
        vol_str = clr(f"🔥VOL({self.analyzer.volume_spike_dir})", C.YELLOW) if self.analyzer.volume_spike else ""
        print(f"│  Mom: {clr(f'{mom:+.4f}%', mom_col)}  Δ3s: {clr(f'{delta:+.4f}', d_col)}  Dir: {clr(mdir, mdir_col)}  Score: {clr(f'{score:.0f}', score_col)}  {flat_str}{spoof_str}{vol_str}{' '*(W-90)}│")

        # RSI + BTC + лимиты
        rsi = self.rsi
        rsi_col = C.RED if rsi > 70 else (C.GREEN if rsi < 30 else C.WHITE)
        rsi_str = clr(f"RSI:{rsi:.0f}", rsi_col)
        btc_col = C.GREEN if self._btc_direction == "up" else (C.RED if self._btc_direction == "down" else C.DIM)
        btc_str = clr(f"BTC:{self._btc_price:.0f}({self._btc_direction})", btc_col) if self._btc_price > 0 else ""
        conf_str = clr(f"Conf:{self.confidence_thr:.0f}%", C.CYAN)
        daily_str = clr(f"Сделок:{self._daily_trade_count}/{self._daily_trade_limit}", C.WHITE)
        best_h = self._get_best_hours()
        pause_str = clr(f"⏸️ пауза {int(self._sl_pause_until - time.time())}с", C.YELLOW) if time.time() < self._sl_pause_until else ""
        print(f"│  {rsi_str}  {btc_str}  {conf_str}  {daily_str}  {pause_str}{' '*(W-80)}│")
        if best_h != "нет данных":
            print(f"│  ⭐ Лучшие часы: {clr(best_h, C.GREEN)}{' '*(W-30)}│")

        # Стены
        bw = len(self.analyzer.bid_walls)
        aw = len(self.analyzer.ask_walls)
        mbw = len(self.analyzer.melting_bid)
        maw = len(self.analyzer.melting_ask)
        walls_str = (
            f"Bid walls: {clr(str(bw), C.GREEN)} "
            f"(🔥{mbw} тают)  "
            f"Ask walls: {clr(str(aw), C.RED)} "
            f"(🔥{maw} тают)"
        )
        print(f"│  {walls_str}{' '*(W-len(walls_str)-4)}│")

        # Показать самую большую стену (если есть)
        all_walls = self.analyzer.bid_walls + self.analyzer.ask_walls
        if all_walls:
            biggest = max(all_walls, key=lambda w: w["volume"])
            bc = C.GREEN if biggest["side"] == "bid" else C.RED
            melt_info = f"  🔥 melt={biggest['melt_rate']:.1f}%/s" if biggest["melting"] else ""
            print(f"│  Крупнейшая стена: {clr(biggest['side'].upper(), bc)} @ {biggest['price']:.4f}  vol={biggest['volume']:.4f}  depth={biggest['depth']:.3f}%{melt_info}{' '*(W-90)}│")
        print(f"└{'─'*W}┘")
        print()

        # ─── ПОЗИЦИЯ ───
        print(f"┌{'─'*W}┐")
        if self.position:
            pos = self.position
            pnl = pos.unrealized_pnl
            pnl_c = pnl_color(pnl)
            dir_str = clr("▲ LONG", C.GREEN) if pos.direction == "LONG" else clr("▼ SHORT", C.RED)
            sign = "+" if pnl >= 0 else ""
            print(f"│  {dir_str}  {self.symbol}  @ {pos.entry_price:.4f}  qty={pos.quantity:.6f}{' '*(W-60)}│")
            print(f"│  SL: {clr(f'{pos.stop_loss:.4f}', C.RED)}  TP: {clr(f'{pos.take_profit:.4f}', C.GREEN)}  Возраст: {pos.age:.0f}s  Причина: {clr(pos.reason, C.CYAN)}  conf={pos.confidence:.0f}%{' '*(W-90)}│")
            print(f"│  Unrealized PnL: {clr(f'{sign}{pnl:.4f} USDT ({sign}{pos.pnl_pct:.2f}%)', C.BOLD+pnl_c)}{' '*(W-50)}│")
        else:
            risk_ok = self._can_trade()
            status = clr("✅ Ожидание сигнала...", C.DIM) if risk_ok else clr("⛔ Лимит риска — торговля на паузе", C.YELLOW)
            print(f"│  Позиция: нет   {status}{' '*(W-50)}│")
        print(f"└{'─'*W}┘")
        print()

        # ─── СТАТИСТИКА ───
        print(f"┌{'─'*W}┐")
        n = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.is_win)
        losses = n - wins
        wr = self.win_rate
        pf = self.profit_factor
        dd = self.max_drawdown
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        print(f"│  Сделок: {clr(str(n), C.BOLD)}  ✅ {wins}  ❌ {losses}  WinRate: {clr(f'{wr:.1f}%', C.BOLD+pnl_color(wr-50))}{' '*(W-60)}│")
        # MTF + динамический риск
        tf_col = C.GREEN if self.tf_agreement == "long" else (C.RED if self.tf_agreement == "short" else C.DIM)
        mult_col = C.GREEN if self._streak_multiplier > 1 else (C.RED if self._streak_multiplier < 1 else C.WHITE)
        sr_str = ""
        if self._support_levels:
            sr_str += f"S:{self._support_levels[-1]:.1f} "
        if self._resistance_levels:
            sr_str += f"R:{self._resistance_levels[0]:.1f}"
        print(f"│  MTF: {clr(self.tf_agreement, tf_col)}  Risk×{clr(f'{self._streak_multiplier:.1f}', mult_col)}  {sr_str}{' '*(W-50)}│")
        print(f"│  PF: {pf_str}  MaxDD: {dd:.4f} USDT  Серия убытков: {self.consec_losses}/{self.max_consec}  Сигналов: {self.signals_generated}{' '*(W-70)}│")
        print(f"│  Uptime: {h:02d}:{m:02d}:{s:02d}  Обновлений стакана: {self.analyzer.update_count}{' '*(W-50)}│")
        print(f"└{'─'*W}┘")
        print()

        # ─── ПОСЛЕДНИЕ 3 СДЕЛКИ ───
        if self.closed_trades:
            print(f"┌{'─'*W}┐")
            print(f"│  {clr('Последние сделки:', C.BOLD)}{' '*(W-20)}│")
            for t in self.closed_trades[-3:][::-1]:
                sign = "+" if t.net_pnl >= 0 else ""
                col  = C.GREEN if t.is_win else C.RED
                icon = "✅" if t.is_win else "❌"
                dir_short = "L" if t.pos.direction == "LONG" else "S"
                print(f"│  {icon} {dir_short} {t.pos.entry_price:.4f}→{t.exit_price:.4f}  {t.exit_reason}  {clr(f'{sign}{t.net_pnl:.4f}', col)} USDT  {t.duration:.0f}s{' '*(W-70)}│")
            print(f"└{'─'*W}┘")
            print()

        # ─── СОБЫТИЯ ───
        print(f"┌{'─'*W}┐")
        print(f"│  {clr('События:', C.BOLD)}{' '*(W-12)}│")
        for event in list(self.event_log)[-6:]:
            # Обрезать до ширины
            plain = event.replace(C.RESET,'').replace(C.GREEN,'').replace(C.RED,'').replace(C.YELLOW,'').replace(C.CYAN,'').replace(C.BOLD,'').replace(C.DIM,'').replace(C.WHITE,'')
            pad = max(0, W - 4 - max(0, len(plain) - 2))
            print(f"│  {event}{' '*pad}│")
        print(f"└{'─'*W}┘")
        print()
        print(clr("  [Ctrl+C для выхода]", C.DIM))

    # ──────────────────────────────────────────
    # Главный цикл
    # ──────────────────────────────────────────

    async def run(self):
        """Запустить демо-движок."""
        self.running = True
        self.start_time = time.time()

        # Загрузить историю прошлых сессий
        self._load_history()

        print(f"\n{clr('CONFI Scalper — Демо-режим', C.BOLD+C.CYAN)}")
        print(f"Символ:  {clr(self.symbol, C.WHITE+C.BOLD)}")
        print(f"Баланс:  {clr(f'{self.balance:.2f} USDT', C.WHITE+C.BOLD)}")
        print(f"Подключение к MEXC WebSocket...")
        print(f"{clr('Данные реальные, сделки виртуальные.', C.YELLOW)}\n")

        # Запуск WS в фоне
        ws_task = asyncio.create_task(self.connect_and_run())

        # Отправить приветствие в TG
        await self._tg(
            f"🤖 <b>Бот запущен!</b>\n"
            f"Символ: {self.symbol}\n"
            f"Баланс: {self.balance:.2f} USDT\n"
            f"Команды: /help"
        )

        # Дашборд-цикл + TG команды
        tg_poll_counter = 0
        try:
            while self.running:
                self.render_dashboard()

                # Проверять TG команды каждые 3 сек
                tg_poll_counter += 1
                if tg_poll_counter % 6 == 0:
                    await self._tg_poll_commands()

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            await self._final_report()

    async def _final_report(self):
        """Финальный отчёт при выходе."""
        print("\n\n" + "="*70)
        print(clr("  ФИНАЛЬНЫЙ ОТЧЁТ ДЕМО-СЕССИИ", C.BOLD+C.CYAN))
        print("="*70)
        print(f"  Символ:           {self.symbol}")
        print(f"  Стартовый баланс: {self.start_balance:.4f} USDT")
        print(f"  Финальный баланс: {self.balance:.4f} USDT")
        pnl = self.total_pnl
        print(f"  Итоговый PnL:     {clr(f'{pnl:+.4f} USDT ({pnl/self.start_balance*100:+.2f}%)', pnl_color(pnl)+C.BOLD)}")
        print(f"  Всего сделок:     {len(self.closed_trades)}")
        print(f"  Win Rate:         {self.win_rate:.1f}%")
        print(f"  Profit Factor:    {self.profit_factor:.2f}")
        print(f"  Max Drawdown:     {self.max_drawdown:.4f} USDT")
        print(f"  Сигналов:         {self.signals_generated}")
        print(f"  WS сообщений:     {self.ws_messages}")
        uptime = time.time() - self.start_time
        h,rem = divmod(int(uptime), 3600)
        m,s   = divmod(rem, 60)
        print(f"  Время работы:     {h:02d}:{m:02d}:{s:02d}")
        print("="*70)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="CONFI Scalper — Демо с реальными данными MEXC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python demo.py                        # BTCUSDT, 1000 USDT
  python demo.py --symbol ETHUSDT       # ETH
  python demo.py --balance 5000 --confidence 60
  python demo.py --symbol SOLUSDT --sl 1.5 --tp 2.5 --wall 4.0
        """,
    )
    p.add_argument("--symbol",     default="BTCUSDT",  help="Торговый символ (default: BTCUSDT)")
    p.add_argument("--balance",    type=float, default=1000.0, help="Стартовый виртуальный баланс USDT (default: 1000)")
    p.add_argument("--risk",       type=float, default=2.0,    help="Риск на сделку %% (default: 2.0)")
    p.add_argument("--sl",         type=float, default=1.0,    help="Стоп-лосс %% (default: 1.0)")
    p.add_argument("--tp",         type=float, default=2.0,    help="Тейк-профит %% (default: 2.0)")
    p.add_argument("--confidence", type=float, default=70.0,   help="Порог уверенности %% (default: 70)")
    p.add_argument("--wall",       type=float, default=4.0,    help="Порог стены (volume/avg, default: 4.0)")
    p.add_argument("--melt",       type=float, default=5.0,    help="Порог таяния %%/s (default: 5.0)")
    p.add_argument("--max-loss",   type=float, default=5.0,    help="Макс. дневной убыток %% (default: 5.0)")
    p.add_argument("--max-losses", type=int,   default=10,     help="Макс. убытков подряд (default: 10)")
    p.add_argument("--commission", type=float, default=0.001,  help="Комиссия биржи (default: 0.001 = 0.1%%)")
    p.add_argument("--depth",      type=int,   default=20,     choices=[5, 10, 20], help="Уровней стакана (default: 20)")
    return p.parse_args()


async def main():
    args = parse_args()

    engine = DemoEngine(
        symbol          = args.symbol,
        balance         = args.balance,
        risk_pct        = args.risk,
        sl_pct          = args.sl,
        tp_pct          = args.tp,
        confidence      = args.confidence,
        wall_threshold  = args.wall,
        melt_threshold  = args.melt,
        max_daily_loss_pct = args.max_loss,
        max_consec_losses  = args.max_losses,
        commission_rate    = args.commission,
        depth_level        = args.depth,
    )

    try:
        await engine.run()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
