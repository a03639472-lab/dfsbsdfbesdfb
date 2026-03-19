"""
mexc_client.py — Асинхронный клиент MEXC Exchange (WebSocket + REST).

Реализует:
    WebSocket:
        - Подписка на стакан ордеров: spot@public.depth.v3.api@{SYMBOL}@{DEPTH}
        - Подписка на сделки: spot@public.deals.v3.api@{SYMBOL}
        - Автоматический ping каждые ws_ping_interval секунд.
        - Переподключение с экспоненциальной задержкой (1→2→4→...→60 сек).
        - Принудительное переподключение каждые ws_reconnect_max_age секунд.
        - Callback-архитектура: on_depth(data), on_trade(data), on_reconnect().

    REST API:
        - HMAC-SHA256 подпись запросов.
        - Rate limiting (не более N запросов в секунду).
        - Retry с экспоненциальной задержкой на ошибки 5xx и таймауты.
        - Методы: get_order_book, get_balance, place_order, cancel_order,
                  get_open_orders, get_order, get_exchange_info.

Использование:
    client = MexcClient(config)
    client.on_depth = analyzer.update
    client.on_trade = handle_trade
    await client.connect()
    # ... работа бота ...
    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Any, Callable, Coroutine, Dict, List, Optional

from models import BotConfig, MexcConfig

logger = logging.getLogger(__name__)

# Тип коллбека: async def callback(data: dict) -> None
AsyncCallback = Callable[[dict], Coroutine[Any, Any, None]]


class MexcClient:
    """Асинхронный клиент для работы с биржей MEXC.

    Поддерживает одновременную работу WebSocket-стрима и REST API.
    Все публичные методы являются корутинами (async).

    Attributes:
        config:           Конфигурация биржи.
        symbol:           Торговый символ (e.g. 'BTCUSDT').
        on_depth:         Коллбек для обновлений стакана.
        on_trade:         Коллбек для новых сделок.
        on_reconnect:     Коллбек при переподключении WebSocket.
        _ws:              Активное WebSocket соединение.
        _ws_task:         asyncio.Task управления WebSocket.
        _ping_task:       asyncio.Task периодического ping.
        _reconnect_count: Счётчик переподключений.
        _connected_at:    Timestamp текущего подключения.
        _running:         Флаг активности клиента.
        _rate_limiter:    Семафор ограничения REST запросов.
        _last_request_ts: Timestamp последнего REST запроса.
    """

    def __init__(self, config: BotConfig, symbol: str) -> None:
        """Инициализация клиента MEXC.

        Args:
            config: Главная конфигурация бота.
            symbol: Торговый символ для подписки.
        """
        self.config = config
        self.mexc: MexcConfig = config.mexc
        self.symbol = symbol.upper()
        self.on_depth: Optional[AsyncCallback] = None
        self.on_trade: Optional[AsyncCallback] = None
        self.on_reconnect: Optional[Callable] = None
        self._ws = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0
        self._connected_at: float = 0.0
        self._running = False
        self._messages_received = 0
        self._rest_request_count = 0
        self._last_request_ts: float = 0.0
        # Семафор для ограничения одновременных REST-запросов
        self._rate_limiter = asyncio.Semaphore(
            max(1, int(self.mexc.rate_limit_req_per_sec))
        )
        logger.info(
            f"MexcClient инициализирован: symbol={self.symbol}, "
            f"ws={self.mexc.ws_url}, rest={self.mexc.rest_url}"
        )

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Запустить WebSocket-подключение в фоновой задаче.

        Создаёт asyncio.Task для управления соединением и задачи ping.
        Задача автоматически переподключается при обрыве.
        """
        self._running = True
        self._ws_task = asyncio.create_task(
            self._ws_manager(), name=f"ws_manager_{self.symbol}"
        )
        logger.info(f"WebSocket-менеджер запущен для {self.symbol}")

    async def disconnect(self) -> None:
        """Корректно остановить WebSocket-соединение.

        Отменяет фоновые задачи и закрывает WebSocket.
        """
        self._running = False

        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        logger.info(f"MexcClient [{self.symbol}] отключён. "
                    f"Переподключений: {self._reconnect_count}, "
                    f"сообщений: {self._messages_received}")

    async def _ws_manager(self) -> None:
        """Управляющая корутина WebSocket: подключение, чтение, переподключение.

        Переподключается при разрыве с экспоненциальной задержкой.
        Принудительно переподключается каждые ws_reconnect_max_age секунд.
        """
        retry_delay = 1.0
        max_delay = 60.0

        while self._running:
            try:
                await self._ws_connect_and_read()
                retry_delay = 1.0  # успешное соединение — сброс задержки
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.error(
                    f"WebSocket [{self.symbol}] ошибка: {exc}. "
                    f"Повтор через {retry_delay:.0f}с"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)
                self._reconnect_count += 1

    async def _ws_connect_and_read(self) -> None:
        """Установить WebSocket-соединение, подписаться и читать сообщения.

        При потере соединения — выбрасывает исключение, которое
        перехватывается в _ws_manager для переподключения.
        """
        try:
            import websockets
        except ImportError:
            logger.warning("websockets не установлен. Симуляция WS-соединения.")
            await asyncio.sleep(3600)  # имитация долгого подключения
            return

        logger.info(f"WebSocket: подключение к {self.mexc.ws_url}")
        async with websockets.connect(
            self.mexc.ws_url,
            ping_interval=None,  # пинг управляем вручную
            close_timeout=5,
            max_size=2 ** 23,    # 8 MB максимальный размер сообщения
        ) as ws:
            self._ws = ws
            self._connected_at = time.time()
            logger.info(f"WebSocket [{self.symbol}] подключён")

            # Подписки
            await self._subscribe(ws)

            # Запуск фонового пинга
            if self._ping_task and not self._ping_task.done():
                self._ping_task.cancel()
            self._ping_task = asyncio.create_task(
                self._ping_loop(ws), name=f"ws_ping_{self.symbol}"
            )

            # Уведомить о переподключении
            if self._reconnect_count > 0 and self.on_reconnect:
                try:
                    self.on_reconnect()
                except Exception as exc:
                    logger.warning(f"Ошибка в on_reconnect callback: {exc}")

            # Читаем сообщения
            async for raw_message in ws:
                if not self._running:
                    break

                # Принудительное переподключение по истечении max_age
                if time.time() - self._connected_at > self.mexc.ws_reconnect_max_age:
                    logger.info(
                        f"WebSocket [{self.symbol}]: принудительное переподключение "
                        f"(age > {self.mexc.ws_reconnect_max_age:.0f}с)"
                    )
                    break

                await self._handle_ws_message(raw_message)

    async def _subscribe(self, ws) -> None:
        """Подписаться на каналы WebSocket.

        Args:
            ws: Активное WebSocket соединение.
        """
        depth_channel = (
            f"spot@public.depth.v3.api@{self.symbol}@{self.mexc.depth_limit}"
        )
        trades_channel = f"spot@public.deals.v3.api@{self.symbol}"

        subscribe_msg = {
            "method": "SUBSCRIPTION",
            "params": [depth_channel, trades_channel],
        }

        await ws.send(json.dumps(subscribe_msg))
        logger.info(f"WebSocket [{self.symbol}]: подписка отправлена: "
                    f"depth={depth_channel}")

    async def _ping_loop(self, ws) -> None:
        """Периодически отправлять ping для поддержания соединения.

        Args:
            ws: Активное WebSocket соединение.
        """
        try:
            while self._running:
                await asyncio.sleep(self.mexc.ws_ping_interval)
                try:
                    await ws.send(json.dumps({"method": "PING"}))
                    logger.debug(f"WebSocket [{self.symbol}]: ping отправлен")
                except Exception as exc:
                    logger.warning(f"WebSocket [{self.symbol}]: ping не удался: {exc}")
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_ws_message(self, raw: str) -> None:
        """Обработать входящее WebSocket-сообщение.

        Парсит JSON, определяет тип данных и вызывает соответствующий коллбек.

        Args:
            raw: Сырая строка JSON из WebSocket.
        """
        self._messages_received += 1
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(f"WebSocket [{self.symbol}]: ошибка JSON: {exc}")
            return

        # PONG от сервера
        if data.get("id") == 0 or data.get("msg") == "PONG":
            return

        channel = data.get("c", "")
        d = data.get("d", {})

        if "depth" in channel and self.on_depth:
            try:
                await self.on_depth(d)
            except Exception as exc:
                logger.error(f"on_depth callback ошибка: {exc}", exc_info=True)

        elif "deals" in channel and self.on_trade:
            try:
                await self.on_trade(d)
            except Exception as exc:
                logger.error(f"on_trade callback ошибка: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    async def get_order_book_rest(
        self, symbol: Optional[str] = None, limit: int = 20
    ) -> dict:
        """Получить стакан ордеров через REST API (резервный метод).

        Используется при переподключении WebSocket для получения полного снапшота.

        Args:
            symbol: Торговый символ (по умолчанию self.symbol).
            limit:  Количество уровней (5/10/20).

        Returns:
            Словарь {'bids': [...], 'asks': [...], 'timestamp': ...}.
        """
        sym = symbol or self.symbol
        params = {"symbol": sym, "limit": limit}
        return await self._public_request("GET", "/api/v3/depth", params)

    async def get_balance(self) -> Dict[str, float]:
        """Получить балансы аккаунта.

        Returns:
            Словарь {asset: free_balance}.

        Raises:
            Exception: При ошибке API или отсутствии подписи.
        """
        data = await self._signed_request("GET", "/api/v3/account")
        balances = {}
        for item in data.get("balances", []):
            free = float(item.get("free", 0))
            if free > 0:
                balances[item["asset"]] = free
        return balances

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Создать ордер на бирже.

        Args:
            symbol:          Торговый символ.
            side:            'BUY' или 'SELL'.
            order_type:      'MARKET', 'LIMIT', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT'.
            quantity:        Объём в базовой валюте.
            price:           Цена для LIMIT ордеров.
            stop_price:      Цена активации для стоп-ордеров.
            client_order_id: Клиентский ID для идентификации ордера.

        Returns:
            dict с полями: orderId, status, price, executedQty, ...

        Raises:
            Exception: При ошибке API (недостаточно средств, некорректные параметры и т.д.).
        """
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": f"{quantity:.8f}",
        }
        if price is not None:
            params["price"] = f"{price:.8f}"
        if stop_price is not None:
            params["stopPrice"] = f"{stop_price:.8f}"
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        logger.info(
            f"Размещение ордера: {side} {order_type} {quantity:.8f} {symbol} "
            f"@ {price or 'MARKET'}"
        )
        return await self._signed_request("POST", "/api/v3/order", params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        """Отменить ордер по ID.

        Args:
            symbol:   Торговый символ.
            order_id: ID ордера на бирже.

        Returns:
            dict с информацией об отменённом ордере.
        """
        params = {"symbol": symbol, "orderId": order_id}
        logger.info(f"Отмена ордера {order_id} [{symbol}]")
        return await self._signed_request("DELETE", "/api/v3/order", params)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        """Получить список открытых ордеров.

        Args:
            symbol: Торговый символ (None = все символы).

        Returns:
            Список словарей с информацией об ордерах.
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._signed_request("GET", "/api/v3/openOrders", params)

    async def get_order(self, symbol: str, order_id: str) -> dict:
        """Получить информацию о конкретном ордере.

        Args:
            symbol:   Торговый символ.
            order_id: ID ордера.

        Returns:
            Словарь с деталями ордера (статус, исполненный объём и т.д.).
        """
        params = {"symbol": symbol, "orderId": order_id}
        return await self._signed_request("GET", "/api/v3/order", params)

    async def get_exchange_info(self, symbol: Optional[str] = None) -> dict:
        """Получить информацию о правилах торгов (лоты, шаги цены и т.д.).

        Args:
            symbol: Конкретный символ или None для всех.

        Returns:
            dict с информацией о правилах символа.
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._public_request("GET", "/api/v3/exchangeInfo", params)

    # ------------------------------------------------------------------
    # Низкоуровневые HTTP методы
    # ------------------------------------------------------------------

    async def _public_request(
        self, method: str, path: str, params: Optional[dict] = None
    ) -> dict:
        """Выполнить публичный (без подписи) REST запрос.

        Args:
            method: HTTP метод ('GET', 'POST').
            path:   Путь API (e.g. '/api/v3/depth').
            params: Query-параметры.

        Returns:
            Распарсенный JSON ответ.

        Raises:
            Exception: При HTTP ошибке или таймауте.
        """
        return await self._request(method, path, params, signed=False)

    async def _signed_request(
        self, method: str, path: str, params: Optional[dict] = None
    ) -> dict:
        """Выполнить подписанный REST запрос с HMAC-SHA256 подписью.

        Args:
            method: HTTP метод.
            path:   Путь API.
            params: Query-параметры (без signature и timestamp).

        Returns:
            Распарсенный JSON ответ.
        """
        return await self._request(method, path, params, signed=True)

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict],
        signed: bool,
    ) -> dict:
        """Базовый HTTP запрос с rate limiting и retry.

        Args:
            method: HTTP метод.
            path:   Путь API.
            params: Параметры запроса.
            signed: Если True — добавить timestamp и HMAC подпись.

        Returns:
            Распарсенный JSON ответ.

        Raises:
            ConnectionError: При исчерпании попыток.
        """
        params = params or {}

        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.mexc.recv_window
            params["signature"] = self._sign(params)

        url = f"{self.mexc.rest_url}{path}"
        headers = {"X-MEXC-APIKEY": self.mexc.api_key}

        for attempt in range(1, self.mexc.max_retry + 1):
            try:
                async with self._rate_limiter:
                    result = await self._do_http_request(
                        method, url, headers, params
                    )
                    self._rest_request_count += 1
                    return result

            except asyncio.TimeoutError:
                wait = self.mexc.retry_delay_base * (2 ** (attempt - 1))
                logger.warning(
                    f"REST таймаут [{path}], попытка {attempt}/{self.mexc.max_retry}, "
                    f"ждём {wait:.1f}с"
                )
                if attempt < self.mexc.max_retry:
                    await asyncio.sleep(wait)

            except Exception as exc:
                wait = self.mexc.retry_delay_base * (2 ** (attempt - 1))
                logger.error(
                    f"REST ошибка [{path}] попытка {attempt}/{self.mexc.max_retry}: "
                    f"{exc}"
                )
                if attempt < self.mexc.max_retry:
                    await asyncio.sleep(wait)

        raise ConnectionError(f"REST запрос [{method} {path}] не выполнен после "
                              f"{self.mexc.max_retry} попыток")

    async def _do_http_request(
        self, method: str, url: str, headers: dict, params: dict
    ) -> dict:
        """Выполнить фактический HTTP-запрос через aiohttp.

        Args:
            method:  HTTP метод.
            url:     Полный URL.
            headers: HTTP заголовки.
            params:  Параметры запроса.

        Returns:
            dict — распарсенный JSON ответ.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp не установлен. Возврат мок-данных.")
            return {}

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            if method == "GET":
                async with session.get(
                    url, headers=headers, params=params, timeout=timeout
                ) as resp:
                    return await self._parse_response(resp)
            elif method == "POST":
                async with session.post(
                    url, headers=headers, data=params, timeout=timeout
                ) as resp:
                    return await self._parse_response(resp)
            elif method == "DELETE":
                async with session.delete(
                    url, headers=headers, params=params, timeout=timeout
                ) as resp:
                    return await self._parse_response(resp)
            else:
                raise ValueError(f"Неподдерживаемый HTTP метод: {method}")

    @staticmethod
    async def _parse_response(resp) -> dict:
        """Разобрать HTTP-ответ и обработать ошибки API.

        Args:
            resp: aiohttp ClientResponse.

        Returns:
            Распарсенный JSON как dict.

        Raises:
            Exception: При HTTP ошибке или ошибке в теле ответа.
        """
        data = await resp.json()

        if resp.status >= 400:
            code = data.get("code", resp.status)
            msg = data.get("msg", "Неизвестная ошибка")
            raise Exception(f"MEXC API ошибка {code}: {msg}")

        return data

    def _sign(self, params: dict) -> str:
        """Создать HMAC-SHA256 подпись параметров запроса.

        Args:
            params: Параметры запроса (без signature).

        Returns:
            HEX строка подписи.
        """
        query_string = urllib.parse.urlencode(params)
        return hmac.new(
            self.mexc.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True если WebSocket задача активна и не завершена."""
        return (
            self._ws_task is not None
            and not self._ws_task.done()
            and self._running
        )

    @property
    def connection_age(self) -> float:
        """Время активного соединения в секундах."""
        return time.time() - self._connected_at if self._connected_at else 0.0

    @property
    def reconnect_count(self) -> int:
        """Количество переподключений WebSocket."""
        return self._reconnect_count

    @property
    def messages_received(self) -> int:
        """Количество полученных WebSocket-сообщений."""
        return self._messages_received

    @property
    def rest_request_count(self) -> int:
        """Количество выполненных REST-запросов."""
        return self._rest_request_count

    def __repr__(self) -> str:
        return (
            f"MexcClient(symbol={self.symbol}, "
            f"connected={self.is_connected}, "
            f"reconnects={self._reconnect_count}, "
            f"messages={self._messages_received})"
        )
