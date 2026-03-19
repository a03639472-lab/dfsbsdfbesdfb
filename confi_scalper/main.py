"""
main.py — Точка входа CONFI Scalper.

Загружает конфигурацию из YAML и переменных окружения,
настраивает логирование, запускает оркестратор.

Запуск:
    python main.py                        # Конфиг из config.yaml
    python main.py --config prod.yaml     # Другой конфиг
    python main.py --dry-run              # Симуляция (без реальных ордеров)
    python main.py --symbol ETHUSDT       # Другой символ

Переменные окружения (из .env файла или системы):
    MEXC_API_KEY        — API ключ биржи MEXC
    MEXC_API_SECRET     — API секрет биржи MEXC
    TELEGRAM_BOT_TOKEN  — Токен Telegram бота
    TELEGRAM_CHAT_ID    — ID Telegram чата для уведомлений
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Добавить корень проекта в путь
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config(config_path: str, overrides: dict) -> "BotConfig":
    """Загрузить и валидировать конфигурацию бота.

    Читает YAML файл, подставляет переменные окружения в формате ${ENV_VAR},
    применяет CLI-переопределения.

    Args:
        config_path: Путь к YAML файлу конфигурации.
        overrides:   Переопределения из командной строки.

    Returns:
        BotConfig с полной конфигурацией.

    Raises:
        FileNotFoundError: Если YAML файл не найден.
        ValueError:        Если конфигурация некорректна.
    """
    import re
    import yaml
    from models import (
        AnalyzerConfig, BotConfig, LoggingConfig,
        MexcConfig, RiskConfig, TelegramConfig,
    )

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")

    with open(config_file, "r", encoding="utf-8") as f:
        raw = f.read()

    # Подстановка переменных окружения ${ENV_VAR}
    def replace_env(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name, "")
        if not value:
            print(f"[WARNING] Переменная окружения {var_name} не установлена")
        return value

    raw = re.sub(r"\$\{([^}]+)\}", replace_env, raw)
    data = yaml.safe_load(raw)

    # Применить CLI-переопределения
    if overrides.get("dry_run"):
        data.setdefault("bot", {})["dry_run"] = True
    if overrides.get("symbol"):
        data.setdefault("bot", {})["symbol"] = overrides["symbol"]

    bot_cfg = data.get("bot", {})
    mexc_cfg = data.get("mexc", {})
    analyzer_cfg = data.get("analyzer", {})
    risk_cfg = data.get("risk", {})
    tg_cfg = data.get("telegram", {})
    log_cfg = data.get("logging", {})

    return BotConfig(
        symbol=bot_cfg.get("symbol", "BTCUSDT"),
        confidence_threshold=bot_cfg.get("confidence_threshold", 70.0),
        loop_interval_seconds=bot_cfg.get("loop_interval_seconds", 1.0),
        dry_run=bot_cfg.get("dry_run", False),
        mexc=MexcConfig(
            api_key=mexc_cfg.get("api_key", os.environ.get("MEXC_API_KEY", "")),
            api_secret=mexc_cfg.get("api_secret", os.environ.get("MEXC_API_SECRET", "")),
            testnet=mexc_cfg.get("testnet", True),
            rest_url=mexc_cfg.get("rest_url", "https://api.mexc.com"),
            ws_url=mexc_cfg.get("ws_url", "wss://wbs-api.mexc.com/ws"),
            depth_limit=mexc_cfg.get("depth_limit", 20),
            recv_window=mexc_cfg.get("recv_window", 5000),
        ),
        analyzer=AnalyzerConfig(
            wall_threshold=analyzer_cfg.get("wall_threshold", 5.0),
            melt_speed_threshold=analyzer_cfg.get("melt_speed_threshold", 10.0),
            imbalance_levels=analyzer_cfg.get("imbalance_levels", 10),
            history_size=analyzer_cfg.get("history_size", 10),
            min_wall_depth=analyzer_cfg.get("min_wall_depth", 0.05),
            max_wall_depth=analyzer_cfg.get("max_wall_depth", 2.0),
            spread_max_pct=analyzer_cfg.get("spread_max_pct", 0.1),
        ),
        risk=RiskConfig(
            risk_per_trade_percent=risk_cfg.get("risk_per_trade_percent", 1.0),
            max_daily_loss_percent=risk_cfg.get("max_daily_loss_percent", 5.0),
            max_consecutive_losses=int(risk_cfg.get("max_consecutive_losses", 3)),
            stop_loss_percent=risk_cfg.get("stop_loss_percent", 2.0),
            take_profit_percent=risk_cfg.get("take_profit_percent", 3.0),
            max_open_positions=int(risk_cfg.get("max_open_positions", 1)),
            min_risk_reward=risk_cfg.get("min_risk_reward", 1.5),
            max_position_age_sec=risk_cfg.get("max_position_age_sec", 3600.0),
            trailing_stop_enabled=risk_cfg.get("trailing_stop_enabled", False),
            trailing_stop_pct=risk_cfg.get("trailing_stop_pct", 1.0),
            commission_rate=risk_cfg.get("commission_rate", 0.001),
        ),
        telegram=TelegramConfig(
            enabled=tg_cfg.get("enabled", True),
            bot_token=tg_cfg.get("bot_token", os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            chat_id=tg_cfg.get("chat_id", os.environ.get("TELEGRAM_CHAT_ID", "")),
            notify_signals=tg_cfg.get("notify_signals", True),
            notify_trades=tg_cfg.get("notify_trades", True),
            notify_errors=tg_cfg.get("notify_errors", True),
            daily_report_hour=int(tg_cfg.get("daily_report_hour", 23)),
        ),
        logging=LoggingConfig(
            level=log_cfg.get("level", "INFO"),
            log_file=log_cfg.get("log_file", "logs/bot.log"),
            db_file=log_cfg.get("db_file", "data/trades.db"),
            rotation=log_cfg.get("rotation", "10 MB"),
            retention=log_cfg.get("retention", "30 days"),
            log_to_console=log_cfg.get("log_to_console", True),
            json_logs=log_cfg.get("json_logs", False),
        ),
    )


def parse_args() -> argparse.Namespace:
    """Разобрать аргументы командной строки.

    Returns:
        argparse.Namespace с разобранными аргументами.
    """
    parser = argparse.ArgumentParser(
        description="CONFI Scalper — Торговый бот для MEXC (скальпинг по стакану)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Путь к YAML конфигурации (по умолчанию: config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Режим симуляции — реальные ордера не размещаются",
    )
    parser.add_argument(
        "--symbol", default=None,
        help="Переопределить торговый символ (e.g. ETHUSDT)",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Уровень логирования",
    )
    return parser.parse_args()


async def main() -> None:
    """Главная async-функция. Загружает конфиг и запускает оркестратор.

    Выполняет:
        1. Загрузку .env файла (если есть).
        2. Разбор аргументов командной строки.
        3. Загрузку YAML конфигурации.
        4. Настройку логирования.
        5. Запуск Orchestrator.run().
    """
    # Загрузить .env если существует
    try:
        from dotenv import load_dotenv
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            print(f"[INFO] Загружен .env файл: {env_file}")
    except ImportError:
        pass

    args = parse_args()
    overrides = {"dry_run": args.dry_run, "symbol": args.symbol}

    # Загрузка конфига
    try:
        config = load_config(args.config, overrides)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Ошибка загрузки конфига: {exc}")
        sys.exit(1)

    # Переопределение уровня логирования
    if args.log_level:
        config.logging.level = args.log_level

    # Настройка логирования
    from modules.logger_stats import setup_logging
    setup_logging(config.logging)

    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Конфигурация загружена: {config}")

    if config.dry_run:
        logger.warning("=" * 50)
        logger.warning("РЕЖИМ СИМУЛЯЦИИ (DRY-RUN) — реальные ордера НЕ размещаются!")
        logger.warning("=" * 50)

    # Запуск оркестратора
    from orchestrator import Orchestrator
    bot = Orchestrator(config)

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Получен KeyboardInterrupt")
    except Exception as exc:
        logger.critical(f"Необработанное исключение: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
