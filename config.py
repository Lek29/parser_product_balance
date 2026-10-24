"""
Модуль конфигурации приложения.

Загружает все переменные окружения из файла .env и предоставляет
их в виде типизированных констант для использования во всём проекте.
"""

import os
from typing import Optional

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
# Префикс stem архива остатков (без учёта регистра в mail_fetcher).
# Смена шаблона имени у отправителя: env и/или парсер даты в mail_fetcher
# (_parse_date_after_stock_prefix, _RE_STOCK_*).
MAIL_STOCK_ARCHIVE_PREFIX: str = os.getenv("MAIL_STOCK_ARCHIVE_PREFIX", "Остатки от ")


def _email_imap_folder() -> str:
    """
    Имя IMAP-папки для поиска архивов остатков.

    Пустая строка в окружении трактуется как ``INBOX``.

    :returns: Имя папки (например ``INBOX`` или ``Stock``).
    :rtype: str
    """
    raw = os.getenv("EMAIL_IMAP_FOLDER", "INBOX")
    stripped = str(raw).strip()
    return stripped if stripped else "INBOX"


def _mail_imap_max_message_age_days() -> Optional[int]:
    """
    Максимальный «возраст» письма по заголовку ``Date`` для раннего выхода при обходе.

    Значения ``0`` и отрицательные означают: окно **отключено**, перебирать все письма
    в папке. По умолчанию ``7`` календарных дней.

    :returns: Положительное число дней или ``None``, если ограничение отключено.
    :rtype: Optional[int]
    :raises EnvironmentError: Если переменная задана непустой строкой, но не целым.
    """
    raw = os.getenv("MAIL_IMAP_MAX_MESSAGE_AGE_DAYS", "7")
    stripped = str(raw).strip()
    if not stripped:
        return 7
    try:
        v = int(stripped, 10)
    except ValueError as e:
        raise EnvironmentError(
            f"Переменная MAIL_IMAP_MAX_MESSAGE_AGE_DAYS должна быть целым числом, получено: {raw!r}"
        ) from e
    if v <= 0:
        return None
    return v


# IMAP-папка с письмами об остатках (см. ``docs/mail-prerequisites.md``).
EMAIL_IMAP_FOLDER: str = _email_imap_folder()
# ``None`` — без ограничения по дате письма при обходе (см. ``parser/mail_fetcher``).
MAIL_IMAP_MAX_MESSAGE_AGE_DAYS: Optional[int] = _mail_imap_max_message_age_days()

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
BOT_PASSWORD: str = _require("BOT_PASSWORD")


def _optional_int_env(key: str) -> Optional[int]:
    """
    Читает необязательную целочисленную переменную окружения
    (в т.ч. отрицательный chat id группы).

    Пустая строка и отсутствие ключа трактуются как ``None``.

    :param key: Имя переменной окружения.
    :type key: str
    :returns: Целое значение или ``None``.
    :rtype: Optional[int]
    :raises EnvironmentError: Если значение задано непустым,
                              но не является целым.
    """
    raw = os.getenv(key)
    if raw is None:
        return None
    stripped = str(raw).strip()
    if not stripped:
        return None
    try:
        return int(stripped, 10)
    except ValueError as e:
        raise EnvironmentError(f"Переменная {key} должна быть целым числом или пустой, получено: {raw!r}") from e


# Уведомления о порогах по группе (docs/fourty_role.md).
# Не задано — отправка алертов пропускается.
# Личка: пороги по группам + xlsx «ушли в ноль» (docs/push_message_about_zero.md).
STOCK_LIMIT_ALERT_CHAT_ID: Optional[int] = _optional_int_env("STOCK_LIMIT_ALERT_CHAT_ID")

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
FILTERED_PARTIAL_XLSX_PATH: str = os.path.join(STORAGE_DIR, "filtered_partial.xlsx")
META_JSON_PATH: str = os.path.join(STORAGE_DIR, "meta.json")
DATA_JSON_PATH: str = os.path.join(STORAGE_DIR, "data.json")
AUTH_JSON_PATH: str = os.path.join(STORAGE_DIR, "auth.json")
GROUP_STOCK_LIMITS_JSON_PATH: str = os.path.join(
    STORAGE_DIR,
    "group_stock_limits.json",
)
WEEKLY_SNAPSHOTS_DIR: str = os.path.join(STORAGE_DIR, "weekly_snapshots")
ZERO_SOLD_STATE_JSON_PATH: str = os.path.join(STORAGE_DIR, "zero_sold_state.json")
