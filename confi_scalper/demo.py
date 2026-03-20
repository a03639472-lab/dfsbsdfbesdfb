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
    Возвращает сигналы если есть.
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
        self._lock: Optional[asyncio.Lock] = None  # инициализируется лениво внутри event loop

        # Последний результат анализа
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

        self.update_count += 1
        self.last_update = now
        return True

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
        Сигналы по приоритету:
        1. Тающая стена (лучший сигнал)
        2. Дисбаланс + подтверждение направления (spread OK)
        """
        price = self.mid_price
        if price <= 0:
            return None

        # Фильтр: спред не должен быть слишком большим
        if self.spread_pct > 0.05:
            return None

        # ── Правило 1: тающая ask-стена → LONG
        if self.melting_ask:
            wall = max(self.melting_ask, key=lambda w: w["melt_rate"])
            conf = min(wall["melt_rate"] / 10.0 * 60 + max((self.imbalance - 1) * 10, 0), 100)
            if conf >= confidence_threshold:
                return self._make_signal("LONG", "melting_ask_wall", conf, price, sl_pct, tp_pct, min_rr, wall)

        # ── Правило 2: тающая bid-стена → SHORT
        if self.melting_bid:
            wall = max(self.melting_bid, key=lambda w: w["melt_rate"])
            conf = min(wall["melt_rate"] / 10.0 * 60 + max((1 / self.imbalance - 1) * 10, 0), 100)
            if conf >= confidence_threshold:
                return self._make_signal("SHORT", "melting_bid_wall", conf, price, sl_pct, tp_pct, min_rr, wall)

        # ── Правило 3: дисбаланс bid → LONG
        # Требуем имбаланс И чтобы он держался (не разовый всплеск)
        if self.imbalance >= imb_long_thr and not self.ask_walls:
            conf = min((self.imbalance - imb_long_thr) * 12 + 60, 90)
            if conf >= confidence_threshold:
                return self._make_signal("LONG", "bid_imbalance", conf, price, sl_pct, tp_pct, min_rr)

        # ── Правило 4: дисбаланс ask → SHORT
        if self.imbalance <= imb_short_thr and not self.bid_walls:
            conf = min((imb_short_thr - self.imbalance) / imb_short_thr * 25 + 60, 90)
            if conf >= confidence_threshold:
                return self._make_signal("SHORT", "ask_imbalance", conf, price, sl_pct, tp_pct, min_rr)

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
        self.ws_blocked = False      # True если WS geo-blocked → используем REST
        self.rest_polls = 0          # счётчик REST-запросов стакана

        # Для дашборда: лог последних событий
        self.event_log: deque = deque(maxlen=8)

        # Текущая цена (из сделок)
        self.last_trade_price = 0.0
        self.price_history: deque = deque(maxlen=60)  # последние 60 цен

    def _log_event(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.event_log.append(f"{C.DIM}{ts}{C.RESET} {msg}")

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
        # Обновить позицию по mid-price стакана
        if self.position:
            await self._check_sl_tp(self.analyzer.mid_price)

        # Генерировать сигнал
        if self.position is None:
            if not self._can_trade():
                return
            sig = self.analyzer.generate_signal(
                confidence_threshold=self.confidence_thr,
                sl_pct=self.sl_pct,
                tp_pct=self.tp_pct,
            )
            if sig:
                self.signals_generated += 1
                self.last_signal = sig
                await self._open_position(sig)

    async def _check_sl_tp(self, price: float):
        """Проверить срабатывание SL/TP виртуальной позиции."""
        if self.position is None:
            return
        result = self.position.update_price(price)
        if result:
            exit_price = (
                self.position.stop_loss   if result == "SL" else
                self.position.take_profit if result == "TP" else price
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

        risk_amount = self.balance * self.risk_pct / 100
        quantity    = risk_amount / sig["entry"]
        if quantity <= 0:
            return

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

        user_data = (
            f"Символ: {self.symbol}\n"
            f"Предложенное направление: {sig['direction']}\n"
            f"Цена: {sig['entry']:.4f}\n"
            f"Причина стратегии: {sig['reason']}\n"
            f"Уверенность стратегии: {sig['confidence']:.0f}%\n"
            f"Bid imbalance: {bid_imb_pct:.1f}%\n"
            f"Ask imbalance: {ask_imb_pct:.1f}%\n"
            f"Bid/Ask ratio: {self.analyzer.imbalance:.3f}\n"
            f"Bid объём (топ-10): {self.analyzer.total_bid_vol:.4f}\n"
            f"Ask объём (топ-10): {self.analyzer.total_ask_vol:.4f}\n"
            f"Спред: {self.analyzer.spread_pct:.4f}%\n"
            f"Тающих bid стен: {len(self.analyzer.melting_bid)}\n"
            f"Тающих ask стен: {len(self.analyzer.melting_ask)}\n"
            f"Всего bid стен: {len(self.analyzer.bid_walls)}\n"
            f"Всего ask стен: {len(self.analyzer.ask_walls)}\n"
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
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logging.warning(f"TG error: {e}")

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
            f"Длительность: {trade.duration:.0f}с\n"
            f"Баланс: {self.balance:.4f} USDT"
        )
        self.position = None

    def _can_trade(self) -> bool:
        """Проверить лимиты риска."""
        if self.consec_losses >= self.max_consec:
            return False
        if self.start_balance > 0:
            loss_pct = -self.daily_pnl / self.start_balance * 100
            if loss_pct >= self.max_daily_loss:
                return False
        return True

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

        print(f"\n{clr('CONFI Scalper — Демо-режим', C.BOLD+C.CYAN)}")
        print(f"Символ:  {clr(self.symbol, C.WHITE+C.BOLD)}")
        print(f"Баланс:  {clr(f'{self.balance:.2f} USDT', C.WHITE+C.BOLD)}")
        print(f"Подключение к MEXC WebSocket...")
        print(f"{clr('Данные реальные, сделки виртуальные.', C.YELLOW)}\n")

        # Запуск WS в фоне
        ws_task = asyncio.create_task(self.connect_and_run())

        # Дашборд-цикл
        try:
            while self.running:
                self.render_dashboard()
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
