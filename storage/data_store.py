"""
Модуль хранилища данных приложения.

Отвечает за сохранение и чтение текущих остатков в JSON,
а также за проверку актуальности данных путём сравнения
даты письма в meta.json с последней доступной на почте.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import config
from parser.excel_parser import normalize_top_category_label

logger = logging.getLogger(__name__)

# Кэш списка авторизованных пользователей (синхронизируется с mtime auth.json и после записи).
_auth_ids_cache: Optional[tuple[int, ...]] = None
_auth_cache_mtime: Optional[float] = None


def atomic_write_json(path: str, obj: Any, **dump_kwargs: Any) -> None:
    """
    Атомарно записывает JSON: временный файл в каталоге назначения, затем ``os.replace``.

    Снижает риск обрезанного ``.json`` при аварийном завершении процесса во время записи.

    :param path: Целевой путь (например ``storage/data.json``).
    :type path: str
    :param obj: Объект, сериализуемый ``json.dump``.
    :type obj: Any
    :param dump_kwargs: Аргументы для ``json.dump`` (``indent``, ``ensure_ascii`` и т.д.).
    :type dump_kwargs: Any
    :returns: None
    :raises OSError: При ошибках ФС.
    """
    if "ensure_ascii" not in dump_kwargs:
        dump_kwargs["ensure_ascii"] = False
    abs_path = os.path.abspath(path)
    directory = os.path.dirname(abs_path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        prefix=".json_",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, **dump_kwargs)
        os.replace(tmp_path, abs_path)
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


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
    payload = {
        "updated_at": datetime.now().isoformat(),
        "count": len(items),
        "items": items,
    }
    atomic_write_json(config.DATA_JSON_PATH, payload, indent=2)
    logger.info("Сохранено %d позиций в %s", len(items), config.DATA_JSON_PATH)


def load_items() -> list[dict]:
    """
    Читает все товарные позиции из storage/data.json.

    **Резерв / вспомогательный API:** в рантайме бота источник позиций для логики —
    ``current.xlsx`` (парсинг через :func:`parser.excel_parser.parse_stock`). Поле
    ``items`` в JSON — производный снимок после успешного пайплайна сохранения.

    :returns: Список позиций или пустой список если файл не существует.
    :rtype: list[dict]
    """
    if not os.path.exists(config.DATA_JSON_PATH):
        logger.warning("data.json не найден, возвращаем пустой список.")
        return []
    try:
        with open(config.DATA_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(
            "data.json повреждён (load_items): %s. Возвращаем пустой список.",
            e,
        )
        return []
    if not isinstance(payload, dict):
        return []
    return payload.get("items", [])


def read_meta_json() -> Optional[dict[str, Any]]:
    """
    Единая точка чтения ``storage/meta.json`` для статуса и сравнения дат.

    Используется вместо разрозненных ``open``+``json.load`` в нескольких функциях,
    чтобы логика обработки повреждённого файла была согласована.

    :returns: Словарь метаданных или ``None``, если файла нет, JSON невалиден
              или корень не объект.
    :rtype: Optional[dict[str, Any]]
    """
    if not os.path.exists(config.META_JSON_PATH):
        return None
    try:
        with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("meta.json повреждён (read_meta_json): %s", e)
        return None
    if not isinstance(meta, dict):
        return None
    return meta


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
    meta = read_meta_json()
    if meta is None:
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

    Удаляет: current.xlsx, filtered.xlsx, filtered_partial.xlsx, data.json, meta.json.
    Не затрагивает auth.json — список авторизованных пользователей сохраняется.
    Безопасен: отсутствующие файлы пропускаются без ошибок.

    :returns: None
    :side effects: Удаляет файлы из storage/.

    :example:
    >>> cleanup_old_data()
    """
    files_to_delete = [
        config.CURRENT_XLSX_PATH,
        config.FILTERED_XLSX_PATH,
        config.FILTERED_PARTIAL_XLSX_PATH,
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

    Кэширует результат по ``mtime`` файла, чтобы не читать диск на каждое
    сообщение. После :func:`_save_auth` кэш обновляется явно. Если файл
    изменён извне (другой процесс), при смене ``mtime`` список перечитывается.

    При повреждении файла (пустой, обрезанный, невалидный JSON) логирует
    ошибку и возвращает пустой список — бот продолжает работу, пользователям
    предлагается повторно ввести пароль.

    :returns: Список авторизованных chat_id или пустой список если файл
              не существует или повреждён.
    :rtype: list[int]
    """
    global _auth_ids_cache, _auth_cache_mtime

    path = config.AUTH_JSON_PATH
    if not os.path.exists(path):
        _auth_ids_cache = tuple()
        _auth_cache_mtime = None
        return []

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    if (
        _auth_ids_cache is not None
        and mtime is not None
        and _auth_cache_mtime == mtime
    ):
        return list(_auth_ids_cache)

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        ids = raw.get("authorized", []) if isinstance(raw, dict) else []
        if not isinstance(ids, list):
            ids = []
        out: list[int] = []
        for x in ids:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
    except json.JSONDecodeError as e:
        logger.error(
            "auth.json повреждён и не может быть прочитан: %s. "
            "Все пользователи будут разлогинены до восстановления файла.",
            e,
        )
        _auth_ids_cache = tuple()
        _auth_cache_mtime = mtime
        return []

    _auth_ids_cache = tuple(out)
    _auth_cache_mtime = mtime
    return list(out)


def _save_auth(chat_ids: list[int]) -> None:
    """
    Сохраняет список авторизованных chat_id в storage/auth.json.

    :param chat_ids: Список chat_id для сохранения.
    :type chat_ids: list[int]
    :returns: None
    """
    global _auth_ids_cache, _auth_cache_mtime

    atomic_write_json(
        config.AUTH_JSON_PATH,
        {"authorized": chat_ids},
        ensure_ascii=True,
    )
    _auth_ids_cache = tuple(chat_ids)
    try:
        _auth_cache_mtime = os.path.getmtime(config.AUTH_JSON_PATH)
    except OSError:
        _auth_cache_mtime = None


def _load_group_stock_limits() -> dict[str, int]:
    """
    Читает словарь лимитов по товарным группам из
    ``storage/group_stock_limits.json``.

    Ключи нормализуются через
    :func:`parser.excel_parser.normalize_top_category_label`, чтобы формат
    совпадал с UI и частичной фильтрацией. Повреждённый JSON, несловарный
    корень и невалидные значения лимитов трактуются мягко: пропускаются
    или дают пустой словарь без падения процесса.

    :returns: Словарь ``имя_группы -> лимит`` только с целыми значениями
              ``>= 0``.
    :rtype: dict[str, int]

    :example:
    >>> _load_group_stock_limits()
    {'Велосипеды': 3, 'Без группы': 0}
    """
    path = config.GROUP_STOCK_LIMITS_JSON_PATH
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(
            "group_stock_limits.json повреждён и не может быть прочитан: %s. "
            "Возвращаем пустой словарь лимитов.",
            e,
        )
        return {}

    if not isinstance(raw, dict):
        return {}

    limits_raw = raw.get("limits", raw)
    if not isinstance(limits_raw, dict):
        return {}

    out: dict[str, int] = {}
    for raw_group, raw_limit in limits_raw.items():
        try:
            group = normalize_top_category_label(str(raw_group))
            limit = int(raw_limit)
        except (TypeError, ValueError):
            continue
        if limit < 0:
            continue
        out[group] = limit
    return out


def _save_group_stock_limits(limits_by_group: dict[str, int]) -> None:
    """
    Сохраняет словарь лимитов по товарным группам в
    ``storage/group_stock_limits.json``.

    Перед записью повторно нормализует ключи групп и отбрасывает
    невалидные значения, чтобы файл содержал только безопасные
    для рантайма данные.

    :param limits_by_group: Словарь ``имя_группы -> лимит``.
    :type limits_by_group: dict[str, int]
    :returns: None
    :raises ValueError: Если один из лимитов меньше нуля.
    :side effects: Атомарно перезаписывает
                   ``storage/group_stock_limits.json``.

    :example:
    >>> _save_group_stock_limits({' Велосипеды ': 5})
    """
    normalized_limits: dict[str, int] = {}
    for raw_group, raw_limit in limits_by_group.items():
        group = normalize_top_category_label(str(raw_group))
        limit = int(raw_limit)
        if limit < 0:
            raise ValueError(
                f"Лимит для группы {group!r} не может быть отрицательным."
            )
        normalized_limits[group] = limit

    payload = {
        "updated_at": datetime.now().isoformat(),
        "count": len(normalized_limits),
        "limits": normalized_limits,
    }
    atomic_write_json(
        config.GROUP_STOCK_LIMITS_JSON_PATH,
        payload,
        ensure_ascii=False,
        indent=2,
    )


def list_all_limits() -> dict[str, int]:
    """
    Возвращает все активные лимиты по товарным группам.

    Это и есть реестр «постоянного надзора»: отдельный список не хранится,
    под наблюдением находятся все группы, присутствующие в словаре лимитов.

    :returns: Словарь ``имя_группы -> лимит``.
    :rtype: dict[str, int]

    :example:
    >>> list_all_limits()
    {'Велосипеды': 3}
    """
    return _load_group_stock_limits()


def get_limit(group_key: str) -> Optional[int]:
    """
    Возвращает лимит для одной товарной группы.

    Входное имя группы нормализуется тем же способом, что и ключи
    в UI бота, поэтому запросы вида ``" Велосипеды "`` и
    ``"Велосипеды"`` эквивалентны.

    :param group_key: Имя товарной группы.
    :type group_key: str
    :returns: Лимит группы или ``None``, если лимит не задан.
    :rtype: Optional[int]

    :example:
    >>> get_limit("Велосипеды")
    3
    """
    normalized_group = normalize_top_category_label(group_key)
    return _load_group_stock_limits().get(normalized_group)


def set_limit(group_key: str, value: int) -> None:
    """
    Устанавливает или обновляет лимит для товарной группы.

    Лимит сохраняется между перезапусками приложения. Значение должно быть
    целым числом ``>= 0``; отрицательные значения отклоняются исключением.

    :param group_key: Имя товарной группы.
    :type group_key: str
    :param value: Новый лимит группы.
    :type value: int
    :returns: None
    :raises ValueError: Если ``value`` меньше нуля.
    :side effects: Обновляет ``storage/group_stock_limits.json``.

    :example:
    >>> set_limit("Велосипеды", 5)
    """
    limit = int(value)
    if limit < 0:
        raise ValueError("Лимит не может быть отрицательным.")

    normalized_group = normalize_top_category_label(group_key)
    limits = _load_group_stock_limits()
    limits[normalized_group] = limit
    _save_group_stock_limits(limits)
    logger.info("Для группы %s установлен лимит %d.", normalized_group, limit)


def clear_limit(group_key: str) -> None:
    """
    Удаляет лимит для товарной группы.

    После удаления группа исчезает из словаря лимитов и автоматически считается
    снятой с постоянного надзора. Если лимита не было, функция завершится тихо.

    :param group_key: Имя товарной группы.
    :type group_key: str
    :returns: None
    :side effects: Обновляет ``storage/group_stock_limits.json``.

    :example:
    >>> clear_limit("Велосипеды")
    """
    normalized_group = normalize_top_category_label(group_key)
    limits = _load_group_stock_limits()
    removed = limits.pop(normalized_group, None)
    if removed is None:
        logger.debug("clear_limit: для группы %s лимит не был задан.", normalized_group)
        return
    _save_group_stock_limits(limits)
    logger.info("Лимит для группы %s снят.", normalized_group)


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
    meta = read_meta_json()
    if meta is None:
        return None
    return meta.get("last_check_date")


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
    existing = read_meta_json()
    if existing is not None:
        meta = dict(existing)
    meta["last_check_date"] = date_str
    atomic_write_json(config.META_JSON_PATH, meta, ensure_ascii=False, indent=2)
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
    meta = read_meta_json()
    if meta is None:
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
