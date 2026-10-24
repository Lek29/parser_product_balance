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


def current_xlsx_fingerprint() -> Optional[tuple[float, int]]:
    """
    Снимает отпечаток ``storage/current.xlsx`` (mtime + size) для кэша парсинга.

    :returns: ``(st_mtime, st_size)`` или ``None``, если файла нет.
    :rtype: Optional[tuple[float, int]]
    """
    path = config.CURRENT_XLSX_PATH
    if not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (float(st.st_mtime), int(st.st_size))


def load_data_json_payload() -> Optional[dict[str, Any]]:
    """
    Читает корневой объект ``storage/data.json`` целиком.

    :returns: Словарь или ``None``, если файла нет / JSON битый / корень не object.
    :rtype: Optional[dict[str, Any]]
    """
    if not os.path.exists(config.DATA_JSON_PATH):
        return None
    try:
        with open(config.DATA_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("data.json повреждён (load_data_json_payload): %s", e)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def data_json_matches_current_xlsx() -> bool:
    """
    Проверяет, что ``data.json`` соответствует текущему ``current.xlsx`` по отпечатку.

    Нужны поля ``source_xlsx_mtime`` и ``source_xlsx_size``, записанные при
    последнем полном :func:`save_items`.

    :returns: ``True``, если отпечатки совпадают и в JSON есть непустой ``items``.
    :rtype: bool
    """
    fp = current_xlsx_fingerprint()
    if fp is None:
        return False
    payload = load_data_json_payload()
    if payload is None:
        return False
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return False
    try:
        mtime = float(payload["source_xlsx_mtime"])
        size = int(payload["source_xlsx_size"])
    except (KeyError, TypeError, ValueError):
        return False
    return mtime == fp[0] and size == fp[1]


def filtered_xlsx_ready_for_send() -> bool:
    """
    Готов ли глобальный ``filtered.xlsx`` к отправке без повторного парсинга.

    Условие: файл существует, непустой, и ``data.json`` валиден относительно
    ``current.xlsx`` (тот же отпечаток, что после пайплайна).

    :returns: ``True``, если можно отдать файл с диска как есть.
    :rtype: bool
    """
    path = config.FILTERED_XLSX_PATH
    if not os.path.isfile(path):
        return False
    try:
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    return data_json_matches_current_xlsx()


def is_local_mail_snapshot_fresh_this_week(
    *,
    reference: Optional[datetime] = None,
) -> bool:
    """
    Локальный файл уже скачивали на этой календарной неделе МСК — IMAP можно пропустить.

    Критерий — ``updated_at`` (факт скачивания в ``_save_meta``), не ``last_check_date``
    (проверка почты без нового файла). Иначе «проверили в октябре, файл с апреля»
    ошибочно блокирует IMAP до конца недели.

    Условия:
    * существует ``current.xlsx``;
    * в ``meta.json`` есть ``updated_at``, дата которого в той же неделе
      Пн–Вс (Europe/Moscow), что и ``reference``.

    :param reference: Точка «сейчас» (для тестов); по умолчанию Москва сейчас.
    :type reference: Optional[datetime]
    :returns: ``True``, если скачивание уже было на этой неделе.
    :rtype: bool
    """
    from services.calendar_week import (
        parse_meta_date,
        same_calendar_week,
        today_moscow,
    )

    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        return False
    meta = read_meta_json()
    if meta is None:
        return False
    anchor = parse_meta_date(meta.get("updated_at"))
    if anchor is None:
        return False
    return same_calendar_week(anchor, today_moscow(reference))


def save_items(
    items: list[dict],
    *,
    source_fingerprint: Optional[tuple[float, int]] = None,
) -> None:
    """
    Сохраняет список всех товарных позиций в storage/data.json.

    :param items: Полный список товарных позиций из parse_stock().
    :type items: list[dict]
    :param source_fingerprint: Отпечаток ``current.xlsx`` ``(mtime, size)``;
        если ``None`` — снимается с диска на момент записи (если файл есть).
    :type source_fingerprint: Optional[tuple[float, int]]
    :returns: None

    :example:
    >>> from parser.excel_parser import parse_stock
    >>> from storage.data_store import save_items
    >>> save_items(parse_stock())
    """
    fp = (
        source_fingerprint
        if source_fingerprint is not None
        else current_xlsx_fingerprint()
    )
    payload: dict[str, Any] = {
        "updated_at": datetime.now().isoformat(),
        "count": len(items),
        "items": items,
    }
    if fp is not None:
        payload["source_xlsx_mtime"] = fp[0]
        payload["source_xlsx_size"] = fp[1]
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
    payload = load_data_json_payload()
    if payload is None:
        if not os.path.exists(config.DATA_JSON_PATH):
            logger.warning("data.json не найден, возвращаем пустой список.")
        return []
    items = payload.get("items", [])
    return items if isinstance(items, list) else []


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
        logger.warning(
            "Не удалось сравнить даты писем: %s. Считаем что обновление нужно.", e
        )
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

    if _auth_ids_cache is not None and mtime is not None and _auth_cache_mtime == mtime:
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
            raise ValueError(f"Лимит для группы {group!r} не может быть отрицательным.")
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


def _empty_red_bin_payload() -> dict[str, Any]:
    """Пустой реестр Red Bin: следующий id с 1, позиций нет."""
    return {"next_id": 1, "items": {}}


def _load_red_bin_payload() -> dict[str, Any]:
    """
    Читает ``storage/red_bin.json``. Битый файл даёт пустой реестр, не падает.

    :returns: ``{"next_id", "items"}``. ``items`` — ``id -> запись``.
    :rtype: dict[str, Any]
    """
    path = config.RED_BIN_JSON_PATH
    if not os.path.exists(path):
        return _empty_red_bin_payload()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("red_bin.json повреждён: %s. Пустой реестр.", e)
        return _empty_red_bin_payload()
    if not isinstance(raw, dict):
        return _empty_red_bin_payload()
    items_raw = raw.get("items")
    if not isinstance(items_raw, dict):
        items_raw = {}
    items: dict[str, dict] = {}
    max_id = 0
    for raw_id, rec in items_raw.items():
        if not isinstance(rec, dict):
            continue
        key = str(rec.get("key") or "").strip()
        if not key:
            continue
        try:
            limit = int(rec["limit"])
            rank = int(rec.get("rank") or 0)
            last_qty = int(rec.get("last_qty") or 0)
            item_id = str(int(raw_id))
        except (TypeError, ValueError, KeyError):
            continue
        if limit < 1:
            continue
        items[item_id] = {
            "key": key,
            "group": str(rec.get("group") or ""),
            "brand": str(rec.get("brand") or ""),
            "name": str(rec.get("name") or ""),
            "limit": limit,
            "rank": max(0, min(3, rank)),
            "last_qty": last_qty,
        }
        max_id = max(max_id, int(item_id))
    try:
        next_id = int(raw.get("next_id") or 0)
    except (TypeError, ValueError):
        next_id = 0
    if next_id <= max_id:
        next_id = max_id + 1
    return {"next_id": next_id, "items": items}


def _write_red_bin_payload(payload: dict[str, Any]) -> None:
    """
    Атомарно пишет реестр Red Bin.

    :param payload: ``next_id`` и ``items``.
    :type payload: dict[str, Any]
    :returns: None
    :side effects: Перезаписывает ``storage/red_bin.json``.
    """
    body = {
        "updated_at": datetime.now().isoformat(),
        "next_id": int(payload["next_id"]),
        "items": payload["items"],
    }
    atomic_write_json(config.RED_BIN_JSON_PATH, body, ensure_ascii=False, indent=2)


def list_red_bin() -> dict[str, dict]:
    """
    Все позиции под точечным надзором.

    :returns: ``id -> запись`` (key, group, brand, name, limit, rank, last_qty).
    :rtype: dict[str, dict]

    :example:
    >>> list_red_bin()
    {}
    """
    return _load_red_bin_payload()["items"]


def save_red_bin_items(items: dict[str, dict]) -> None:
    """
    Перезаписывает позиции реестра, не сдвигая ``next_id`` назад.

    Нужна машине стадий после снимка: rank и last_qty уже посчитаны снаружи.

    :param items: Словарь ``id -> запись``, как у :func:`list_red_bin`.
    :type items: dict[str, dict]
    :returns: None
    :side effects: ``storage/red_bin.json``.
    """
    payload = _load_red_bin_payload()
    payload["items"] = items
    _write_red_bin_payload(payload)


def upsert_red_bin(item_fields: dict[str, Any], limit: int) -> str:
    """
    Ставит или меняет лимит одной позиции. Сообщений не шлёт.

    Существующий ключ: меняется только ``limit``, rank пересчитывается
    от уже сохранённого ``last_qty``. Новый ключ получает rank от
    ``item_fields["last_qty"]`` — тихая база на момент выбора.

    :param item_fields: ``key``, ``group``, ``brand``, ``name``, ``last_qty``.
    :type item_fields: dict[str, Any]
    :param limit: Целое ``>= 1``.
    :type limit: int
    :returns: Строковый id записи.
    :rtype: str
    :raises ValueError: Лимит меньше 1 или у новой позиции нет ``last_qty``.
    :side effects: ``storage/red_bin.json``.

    :example:
    >>> upsert_red_bin(
    ...     {"key": "мячи|mikasa|мяч", "group": "Мячи", "brand": "Mikasa",
    ...      "name": "Мяч", "last_qty": 15},
    ...     5,
    ... )
    '1'
    """
    from services.red_bin import zone_rank

    limit_n = int(limit)
    if limit_n < 1:
        raise ValueError("Лимит Red Bin должен быть целым >= 1.")
    key = str(item_fields.get("key") or "").strip()
    if not key:
        raise ValueError("У позиции Red Bin нет ключа.")

    payload = _load_red_bin_payload()
    items: dict[str, dict] = payload["items"]
    existing_id = next((iid for iid, rec in items.items() if rec.get("key") == key), None)
    if existing_id is not None:
        rec = items[existing_id]
        last_qty = int(rec.get("last_qty") or 0)
        rec["limit"] = limit_n
        rec["rank"] = zone_rank(last_qty, limit_n)
        _write_red_bin_payload(payload)
        logger.info("Red Bin id=%s лимит обновлён до %d (без алерта).", existing_id, limit_n)
        return existing_id

    try:
        last_qty = int(item_fields["last_qty"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("Для новой позиции Red Bin нужен last_qty.") from e
    item_id = str(int(payload["next_id"]))
    payload["next_id"] = int(payload["next_id"]) + 1
    items[item_id] = {
        "key": key,
        "group": str(item_fields.get("group") or ""),
        "brand": str(item_fields.get("brand") or ""),
        "name": str(item_fields.get("name") or ""),
        "limit": limit_n,
        "rank": zone_rank(last_qty, limit_n),
        "last_qty": last_qty,
    }
    _write_red_bin_payload(payload)
    logger.info("Red Bin id=%s поставлен на надзор, лимит %d.", item_id, limit_n)
    return item_id


def clear_red_bin(item_id: str) -> None:
    """
    Снимает одну позицию с надзора. Неизвестный id — тихий выход.

    :param item_id: Id из :func:`list_red_bin`.
    :type item_id: str
    :returns: None
    :side effects: ``storage/red_bin.json``, если запись была.
    """
    payload = _load_red_bin_payload()
    items: dict[str, dict] = payload["items"]
    removed = items.pop(str(item_id), None)
    if removed is None:
        return
    _write_red_bin_payload(payload)
    logger.info("Red Bin id=%s снят.", item_id)


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
