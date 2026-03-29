"""
Модуль конфигурации приложения.

Загружает все переменные окружения из файла .env и предоставляет
их в виде типизированных констант для использования во всём проекте.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """
    Возвращает значение переменной окружения, выбрасывая исключение если она не задана.

    :param key: Имя переменной окружения.
    :type key: str
    :returns: Значение переменной окружения.
    :rtype: str
    :raises EnvironmentError: Если переменная не найдена в окружении или в .env.
    """
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Обязательная переменная окружения не задана: {key}")
    return value


# --- Почта ---
EMAIL_ADDRESS: str = _require("EMAIL_ADDRESS")
EMAIL_PASSWORD: str = _require("EMAIL_PASSWORD")
EMAIL_IMAP_SERVER: str = _require("EMAIL_IMAP_SERVER")

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
BOT_PASSWORD: str = _require("BOT_PASSWORD")

# --- Параметры фильтрации ---
STOCK_THRESHOLD: int = int(os.getenv("STOCK_THRESHOLD", "5"))
# Частичная фильтрация (docs/partial_filtering.md, шаг 5):
# Пустой черновик — без STOCK_THRESHOLD; задайте порог хотя бы одной группы.
# 0 позиций после отбора — файл не шлём, только сообщение в чат.

# --- Пути ---
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR: str = os.path.join(BASE_DIR, "temp")
STORAGE_DIR: str = os.path.join(BASE_DIR, "storage")
CURRENT_XLSX_PATH: str = os.path.join(STORAGE_DIR, "current.xlsx")
# Глобальный filtered.xlsx: планировщик, актуализация, «Получить…».
FILTERED_XLSX_PATH: str = os.path.join(STORAGE_DIR, "filtered.xlsx")
# Частичная выгрузка; планировщик (scheduler) этот путь не использует.
FILTERED_PARTIAL_XLSX_PATH: str = os.path.join(
    STORAGE_DIR, "filtered_partial.xlsx"
)
META_JSON_PATH: str = os.path.join(STORAGE_DIR, "meta.json")
DATA_JSON_PATH: str = os.path.join(STORAGE_DIR, "data.json")
AUTH_JSON_PATH: str = os.path.join(STORAGE_DIR, "auth.json")
