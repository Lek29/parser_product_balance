"""
Модуль хранилища данных приложения.

Отвечает за сохранение и чтение текущих остатков в JSON,
а также за проверку актуальности данных путём сравнения
даты письма в meta.json с последней доступной на почте.
"""

import json
import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional

import config

logger = logging.getLogger(__name__)


def save_items(items: list[dict]) -> None:
    """
    Сохраняет список всех товарных позиций в storage/data.json.

    :param items: Полный список товарных позиций из parse_stock().
    :type items: list[dict]
    :returns: None

    :example:
    >>> from parser.excel_parser import parse_stock
    >>> from storage.data_store import save_items
    >>> save_items(parse_stock())
    """
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(),
        "count": len(items),
        "items": items,
    }
    with open(config.DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("Сохранено %d позиций в %s", len(items), config.DATA_JSON_PATH)


def load_items() -> list[dict]:
    """
    Читает все товарные позиции из storage/data.json.

    :returns: Список позиций или пустой список если файл не существует.
    :rtype: list[dict]
    """
    if not os.path.exists(config.DATA_JSON_PATH):
        logger.warning("data.json не найден, возвращаем пустой список.")
        return []
    with open(config.DATA_JSON_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("items", [])


def get_last_update_dt() -> Optional[datetime]:
    """
    Возвращает дату и время последнего обновления данных из meta.json.

    :returns: datetime объект или None если meta.json не существует или повреждён.
    :rtype: Optional[datetime]
    """
    if not os.path.exists(config.META_JSON_PATH):
        return None
    try:
        with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("meta.json повреждён (get_last_update_dt): %s", e)
        return None
    updated_at = meta.get("updated_at")
    if not updated_at:
        return None
    return datetime.fromisoformat(updated_at)


def get_last_email_date_str() -> Optional[str]:
    """
    Возвращает строку даты последнего скачанного письма из meta.json.

    :returns: Строка даты в формате RFC 2822 или None если meta.json не существует
              или повреждён.
    :rtype: Optional[str]

    :example:
    >>> get_last_email_date_str()
    'Mon, 9 Mar 2026 09:00:51 +0800'
    """
    if not os.path.exists(config.META_JSON_PATH):
        return None
    try:
        with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("meta.json повреждён (get_last_email_date_str): %s", e)
        return None
    return meta.get("email_date")


def check_update_available(latest_email_date_str: str) -> bool:
    """
    Сравнивает дату последнего письма с почты с сохранённой в meta.json.

    Используется для определения: есть ли новые данные на почте,
    которые ещё не были скачаны.

    :param latest_email_date_str: Строка даты письма из IMAP (заголовок Date).
    :type latest_email_date_str: str
    :returns: True если на почте есть более новое письмо, False если данные актуальны.
    :rtype: bool

    :example:
    >>> check_update_available("Mon, 16 Mar 2026 09:00:00 +0800")
    True
    """
    saved = get_last_email_date_str()
    if saved is None:
        logger.info("meta.json отсутствует — обновление необходимо.")
        return True

    if saved == latest_email_date_str:
        logger.info("Данные актуальны. Дата письма совпадает: %s", saved)
        return False

    try:
        saved_dt = parsedate_to_datetime(saved)
        latest_dt = parsedate_to_datetime(latest_email_date_str)
        is_newer = latest_dt > saved_dt
        if is_newer:
            logger.info("Доступно обновление: %s → %s", saved, latest_email_date_str)
        else:
            logger.info("Данные актуальны (сохранённое письмо не старше).")
        return is_newer
    except Exception as e:
        logger.warning("Не удалось сравнить даты писем: %s. Считаем что обновление нужно.", e)
        return True


def cleanup_old_data() -> None:
    """
    Удаляет все устаревшие файлы данных перед сохранением новых остатков.

    Удаляет: current.xlsx, filtered.xlsx, data.json, meta.json.
    Не затрагивает auth.json — список авторизованных пользователей сохраняется.
    Безопасен: отсутствующие файлы пропускаются без ошибок.

    :returns: None
    :side effects: Удаляет файлы из storage/.

    :example:
    >>> cleanup_old_data()
    """
    files_to_delete = [
        config.CURRENT_XLSX_PATH,
        os.path.join(config.STORAGE_DIR, "filtered.xlsx"),
        config.DATA_JSON_PATH,
        config.META_JSON_PATH,
    ]
    for path in files_to_delete:
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info("Удалён устаревший файл: %s", path)
            except OSError as e:
                logger.warning("Не удалось удалить %s: %s", path, e)


def _load_auth() -> list[int]:
    """
    Читает список авторизованных chat_id из storage/auth.json.

    При повреждении файла (пустой, обрезанный, невалидный JSON) логирует
    ошибку и возвращает пустой список — бот продолжает работу, пользователям
    предлагается повторно ввести пароль.

    :returns: Список авторизованных chat_id или пустой список если файл
              не существует или повреждён.
    :rtype: list[int]
    """
    if not os.path.exists(config.AUTH_JSON_PATH):
        return []
    try:
        with open(config.AUTH_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("authorized", [])
    except json.JSONDecodeError as e:
        logger.error(
            "auth.json повреждён и не может быть прочитан: %s. "
            "Все пользователи будут разлогинены до восстановления файла.",
            e,
        )
        return []


def _save_auth(chat_ids: list[int]) -> None:
    """
    Сохраняет список авторизованных chat_id в storage/auth.json.

    :param chat_ids: Список chat_id для сохранения.
    :type chat_ids: list[int]
    :returns: None
    """
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    with open(config.AUTH_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"authorized": chat_ids}, f)


def is_authorized(chat_id: int) -> bool:
    """
    Проверяет, авторизован ли пользователь с указанным chat_id.

    Авторизованные пользователи хранятся в storage/auth.json.
    Авторизация сохраняется между перезапусками бота.

    :param chat_id: Telegram chat_id пользователя.
    :type chat_id: int
    :returns: True если пользователь авторизован, False иначе.
    :rtype: bool

    :example:
    >>> is_authorized(123456789)
    False
    """
    return chat_id in _load_auth()


def authorize(chat_id: int) -> None:
    """
    Добавляет пользователя в список авторизованных и сохраняет в auth.json.

    Повторный вызов для уже авторизованного пользователя безопасен — дублей не будет.

    :param chat_id: Telegram chat_id пользователя для авторизации.
    :type chat_id: int
    :returns: None
    :side effects: Обновляет storage/auth.json.

    :example:
    >>> authorize(123456789)
    >>> is_authorized(123456789)
    True
    """
    chat_ids = _load_auth()
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        _save_auth(chat_ids)
        logger.info("Пользователь %d авторизован.", chat_id)
    else:
        logger.debug("Пользователь %d уже был авторизован.", chat_id)


def get_last_check_date() -> Optional[str]:
    """
    Читает дату последней проверки почты из storage/meta.json.

    :returns: Дата в формате 'YYYY-MM-DD' или None если поле/файл отсутствует
              или повреждён.
    :rtype: Optional[str]

    :example:
    >>> get_last_check_date()
    '2026-03-13'
    """
    if not os.path.exists(config.META_JSON_PATH):
        return None
    try:
        with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("last_check_date")
    except json.JSONDecodeError as e:
        logger.error("meta.json повреждён (get_last_check_date): %s", e)
        return None


def set_last_check_date(date_str: str) -> None:
    """
    Записывает дату последней проверки почты в storage/meta.json.

    Если meta.json существует — обновляет только поле last_check_date,
    остальные поля сохраняются. Если файл не существует — создаёт с одним полем.

    :param date_str: Дата в формате 'YYYY-MM-DD'.
    :type date_str: str
    :returns: None
    :side effects: Обновляет storage/meta.json.

    :example:
    >>> from datetime import date
    >>> set_last_check_date(date.today().isoformat())
    """
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    meta: dict = {}
    if os.path.exists(config.META_JSON_PATH):
        try:
            with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except json.JSONDecodeError as e:
            logger.error("meta.json повреждён (set_last_check_date): %s", e)
            meta = {}
    meta["last_check_date"] = date_str
    with open(config.META_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.debug("last_check_date обновлён: %s", date_str)


def get_status_text() -> str:
    """
    Возвращает читаемую строку со статусом данных для отображения в боте.

    :returns: Строка вида "Актуально на 09.03.2026, обновлено 12.03.2026 в 18:05"
              или "Данные ещё не загружены" если файлов нет.
    :rtype: str

    :example:
    >>> print(get_status_text())
    Актуально на 09.03.2026, загружено 12.03.2026 в 18:05
    """
    if not os.path.exists(config.META_JSON_PATH):
        return "Данные ещё не загружены"

    try:
        with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("meta.json повреждён (get_status_text): %s", e)
        return "Данные ещё не загружены"

    email_date_str = meta.get("email_date", "")
    updated_at_str = meta.get("updated_at", "")

    # Если основных полей нет — данные ещё не загружались (только last_check_date)
    if not email_date_str or not updated_at_str:
        return "Данные ещё не загружены"

    try:
        email_dt = parsedate_to_datetime(email_date_str)
        email_formatted = email_dt.strftime("%d.%m.%Y")
    except Exception:
        email_formatted = email_date_str

    try:
        updated_dt = datetime.fromisoformat(updated_at_str)
        updated_formatted = updated_dt.strftime("%d.%m.%Y в %H:%M")
    except Exception:
        updated_formatted = updated_at_str

    return f"Актуально на {email_formatted}, загружено {updated_formatted}"
