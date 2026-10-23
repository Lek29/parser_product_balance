"""
Модуль получения Excel-остатков с корпоративной почты.

Подключается к почтовому серверу по IMAP SSL и открывает папку
:data:`config.EMAIL_IMAP_FOLDER` (по умолчанию ``INBOX``; для рассылок часто
отдельная папка, например ``Stock``, см. ``docs/mail-prerequisites.md``).

Собирает вложения-архивы (.zip / .rar) с именем по префиксу
:data:`config.MAIL_STOCK_ARCHIVE_PREFIX` (по умолчанию «Остатки от »),
выбирает архив с максимальной датой в имени файла (при неоднозначности —
более новое письмо по заголовку ``Date``), скачивает во ``temp/``,
распаковывает и сохраняет ``.xlsx`` в ``storage/current.xlsx``. Метаданные —
в ``storage/meta.json``.

Если задано положительное :data:`config.MAIL_IMAP_MAX_MESSAGE_AGE_DAYS`
``(N)``, обход писем от новых к старым может **прерваться** без загрузки
полного тела, когда по заголовку ``Date`` письмо однозначно старше ``N``
календарных дней (оптимизация; см. ``docs/fix_mail.md``). При ``None`` (в т.ч.
env ``0`` или отрицательное) окно отключено. Невалидный или пустой ``Date``
**не** используется для раннего выхода — ``RFC822`` подгружается; дата **строго
после** «сегодня» тоже не даёт раннего выхода. Актуальность отчёта по смыслу —
из даты в **имени файла**, не только из этого окна.
"""

import email
import email.message
import imaplib
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

# Дата сразу после префикса: только DD.MM.YYYY или YYYY-MM-DD (без угадывания локали).
_RE_STOCK_FILENAME_DDMMYYYY = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})")
_RE_STOCK_FILENAME_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class _StockArchiveCandidate:
    """
    Вложение-архив остатков и ключи выбора «лучшего» среди писем в выбранной IMAP-папке.

    :ivar msg: Полное RFC822-сообщение (для скачивания вложения).
    :ivar filename: Декодированное имя файла.
    :ivar email_date: Значение заголовка ``Date``.
    :ivar msg_id: Идентификатор письма IMAP (байты), для разрешения ничьих.
    :ivar stock_date: Дата ранжирования (из имени или fallback на дату письма).
    :ivar parsed_filename_date: Дата из имени или ``None``, если использован fallback.
    :ivar email_dt: Разобранный ``Date`` для сравнения писем с одной и той же stock_date.
    """

    msg: email.message.Message
    filename: str
    email_date: str
    msg_id: bytes
    stock_date: date
    parsed_filename_date: Optional[date]
    email_dt: datetime


class MailFetchOutcome(str, Enum):
    """
    Результат :func:`fetch_and_save`: явные семантики вместо ``Optional[bool]``.

    :cvar SKIPPED_TODAY: IMAP не вызывался — сегодня уже была проверка (при ``force=False``).
    :cvar NO_CHANGES: Почта проверена, новых данных нет (нет письма или дата не новее сохранённой).
    :cvar UPDATED: Новый архив скачан, ``current.xlsx`` и meta обновлены.
    :cvar ARCHIVE_FAILURE: Ошибка скачивания вложения или извлечения .xlsx (не путать с NO_CHANGES).
    """

    SKIPPED_TODAY = "skipped_today"
    NO_CHANGES = "no_changes"
    UPDATED = "updated"
    ARCHIVE_FAILURE = "archive_failure"


try:
    import rarfile
    RAR_SUPPORT = True
except ImportError:
    RAR_SUPPORT = False
    logger.warning("Модуль rarfile не установлен — поддержка .rar архивов недоступна.")


def _decode_mime_words(text: str) -> str:
    """
    Декодирует строку заголовка MIME (например, имя файла вложения).

    :param text: Закодированная строка заголовка MIME.
    :type text: str
    :returns: Декодированная строка в UTF-8.
    :rtype: str
    """
    parts = decode_header(text)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _stem_and_ext(filename: str) -> Tuple[str, str]:
    """
    Делит имя файла на основу и расширение (расширение в нижнем регистре).

    :param filename: Имя файла вложения.
    :type filename: str
    :returns: Пара (stem, ext), например (``\"Остатки от 1.1.2026\"``, ``\".zip\"``).
    :rtype: Tuple[str, str]
    """
    base, ext = os.path.splitext(filename)
    return base, ext.lower()


def _is_archive_extension(ext: str) -> bool:
    """
    Проверяет, что расширение — поддерживаемый архив.

    :param ext: Расширение с точкой, в нижнем регистре.
    :type ext: str
    :returns: ``True`` для ``.zip`` / ``.rar``.
    :rtype: bool
    """
    return ext in (".zip", ".rar")


def _parse_email_date_for_sort(date_header: str) -> datetime:
    """
    Парсит заголовок ``Date`` для сравнения «какое письмо новее».

    При ошибке или пустой строке возвращает ``datetime.min`` (наивный), чтобы
    такие письма уходили в конец при прочих равных.

    :param date_header: Сырая строка заголовка.
    :type date_header: str
    :returns: Нормализованный наивный локальный ``datetime`` или ``datetime.min``.
    :rtype: datetime
    """
    if not date_header:
        return datetime.min.replace(tzinfo=None)
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (TypeError, ValueError, OverflowError):
        return datetime.min.replace(tzinfo=None)


_MIN_NAIVE_DATETIME = datetime.min.replace(tzinfo=None)


def _scan_should_stop_before_rfc822(
    date_header: str,
    max_age_days: Optional[int],
    today: date,
) -> Tuple[bool, bool]:
    """
    Окно по возрасту письма по заголовку ``Date`` (только оптимизация обхода IMAP).

    Бизнес-дата остатков по-прежнему из имени файла; см. ``docs/fix_mail.md`` §4.
    При ``max_age_days is None`` окно выключено.

    :param date_header: Сырая строка заголовка ``Date`` из ``BODY.PEEK[HEADER]``.
    :param max_age_days: Положительное ``N`` или ``None`` (без ограничения).
    :param today: Календарная дата для сравнения (обычно ``date.today()``).
    :returns: Пара ``(stop_scan, date_unparsed)``. ``stop_scan`` — прервать цикл,
        не вызывая ``fetch(RFC822)`` для этого и последующих писем (письмо однозначно
        старше окна). ``date_unparsed`` — заголовок не разобран (как ``datetime.min``):
        ранний выход **не** применять; вызывающий подгружает ``RFC822`` и пишет warning.
    """
    if max_age_days is None:
        return False, False
    dt = _parse_email_date_for_sort(date_header)
    if dt == _MIN_NAIVE_DATETIME:
        return False, True
    d = dt.date()
    # Дата в будущем: не считаем письмо «старше окна» (ошибка TZ/сервера).
    if d > today:
        return False, False
    if (today - d).days > max_age_days:
        return True, False
    return False, False


def _msg_id_numeric(msg_id: bytes) -> int:
    """
    Числовой UID/msg id для устойчивого сравнения при равных датах.

    :param msg_id: Идентификатор из ответа ``imap.search``.
    :type msg_id: bytes
    :returns: Целое или 0, если не удалось распарсить.
    :rtype: int
    """
    try:
        return int(msg_id.decode("ascii", errors="ignore"))
    except ValueError:
        return 0


def _filename_has_stock_prefix(filename: str) -> bool:
    """
    Проверяет, что нормализованное имя (stem) начинается с префикса остатков.

    Вместе с :func:`_is_archive_extension` — отбор только ``.zip`` / ``.rar`` и stem
    с префиксом :data:`config.MAIL_STOCK_ARCHIVE_PREFIX`. Иной шаблон имени у отправителя:
    обновить env и/или :func:`_parse_date_after_stock_prefix`.

    Сравнение префикса без учёта регистра (``casefold``).

    :param filename: Полное имя файла вложения.
    :type filename: str
    :returns: ``True``, если stem начинается с ``config.MAIL_STOCK_ARCHIVE_PREFIX``.
    :rtype: bool
    """
    stem, _ = _stem_and_ext(filename)
    prefix = config.MAIL_STOCK_ARCHIVE_PREFIX
    if len(stem) < len(prefix):
        return False
    return stem[: len(prefix)].casefold() == prefix.casefold()


def _parse_date_after_stock_prefix(filename: str) -> Optional[date]:
    """
    Извлекает дату из имени файла сразу после префикса «Остатки от ».

    Допустимы только форматы ``DD.MM.YYYY`` и ``YYYY-MM-DD`` в начале хвоста
    (после префикса допускаются пробелы).

    :param filename: Имя вложения после MIME-декодирования.
    :type filename: str
    :returns: Дата или ``None``, если префикс/формат не подошли.
    :rtype: Optional[date]
    """
    stem, _ = _stem_and_ext(filename)
    prefix = config.MAIL_STOCK_ARCHIVE_PREFIX
    if len(stem) < len(prefix):
        return None
    if stem[: len(prefix)].casefold() != prefix.casefold():
        return None
    rest = stem[len(prefix) :].strip()
    m = _RE_STOCK_FILENAME_DDMMYYYY.match(rest)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(yyyy, mm, dd)
        except ValueError:
            return None
    m = _RE_STOCK_FILENAME_ISO.match(rest)
    if m:
        yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(yyyy, mm, dd)
        except ValueError:
            return None
    return None


def _effective_stock_date(
    filename: str,
    email_date_header: str,
) -> Optional[Tuple[date, Optional[date]]]:
    """
    Возвращает дату для ранжирования кандидата и факт распознавания даты в имени.

    Если дата в имени не найдена, используется календарная дата из заголовка
    ``Date`` (с предупреждением в логе). Если и она недоступна — кандидат
    отбрасывается (``None``).

    :param filename: Имя архива.
    :type filename: str
    :param email_date_header: Заголовок ``Date`` письма.
    :type email_date_header: str
    :returns: ``(stock_date, parsed_filename_date)`` или ``None``.
    :rtype: Optional[Tuple[date, Optional[date]]]
    """
    parsed = _parse_date_after_stock_prefix(filename)
    if parsed is not None:
        return parsed, parsed
    edt = _parse_email_date_for_sort(email_date_header)
    if edt == datetime.min.replace(tzinfo=None):
        logger.warning(
            "Архив отброшен (нет даты в имени DD.MM.YYYY/YYYY-MM-DD и нет валидного Date): %s",
            filename,
        )
        return None
    logger.warning(
        "Дата в имени архива не распознана, для сортировки используется дата письма: %s",
        filename,
    )
    return edt.date(), None


def _pick_best_stock_candidate(
    candidates: List[_StockArchiveCandidate],
) -> Optional[_StockArchiveCandidate]:
    """
    Выбирает кандидата с максимальной ``stock_date``.

    При равенстве: более новое письмо (``email_dt``), затем больший IMAP ``msg_id``,
    затем лексикографически большее имя файла (детерминированно при нескольких
    вложениях в одном письме).

    :param candidates: Непустой или пустой список кандидатов.
    :type candidates: List[_StockArchiveCandidate]
    :returns: Лучший кандидат или ``None``, если список пуст.
    :rtype: Optional[_StockArchiveCandidate]
    """
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda c: (
            c.stock_date,
            c.email_dt,
            _msg_id_numeric(c.msg_id),
            c.filename,
        ),
    )


def _connect_imap() -> imaplib.IMAP4_SSL:
    """
    Устанавливает IMAP SSL соединение с почтовым сервером и выполняет авторизацию.

    :returns: Авторизованный объект IMAP4_SSL соединения.
    :rtype: imaplib.IMAP4_SSL
    :raises imaplib.IMAP4.error: При неверных учётных данных или недоступности сервера.
    """
    logger.info("Подключение к IMAP серверу %s...", config.EMAIL_IMAP_SERVER)
    imap = imaplib.IMAP4_SSL(config.EMAIL_IMAP_SERVER, 993)
    imap.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
    logger.info("Авторизация успешна: %s", config.EMAIL_ADDRESS)
    return imap


def _select_mail_folder(imap: imaplib.IMAP4_SSL) -> None:
    """
    Открывает IMAP-папку :data:`config.EMAIL_IMAP_FOLDER` для последующего поиска.

    :param imap: Авторизованное соединение IMAP.
    :type imap: imaplib.IMAP4_SSL
    :raises RuntimeError: Если сервер вернул не ``OK`` на ``SELECT`` (папка отсутствует
        или имя неверно).
    :raises imaplib.IMAP4.error: При ошибке протокола/сети на ``SELECT``.
    """
    folder = config.EMAIL_IMAP_FOLDER
    try:
        typ, data = imap.select(folder)
    except imaplib.IMAP4.error as exc:
        logger.error("Ошибка IMAP SELECT для папки %r: %s", folder, exc)
        raise
    if typ != "OK":
        detail = ""
        if data:
            first = data[0]
            detail = (
                first.decode("utf-8", errors="replace").strip()
                if isinstance(first, bytes)
                else str(first)
            )
        logger.error(
            "Не удалось открыть IMAP-папку %r (ответ сервера: %s). "
            "Проверьте EMAIL_IMAP_FOLDER.",
            folder,
            detail or typ,
        )
        raise RuntimeError(
            f"Не удалось открыть IMAP-папку {folder!r}: {detail or typ}"
        )


def _imap_fetch_payload(fetch_data: Optional[list]) -> Optional[bytes]:
    """
    Извлекает байтовое тело из ответа ``imap.fetch``.

    При наличии literal ``imaplib`` возвращает элементы вида
    ``(b'… {N}', b'payload…')``. Без literal (например ``BODY[HEADER] NIL``)
    первый элемент часто бывает голыми ``bytes``: тогда ``data[0][1]`` — это
    ``int`` (код байта), и ``email.message_from_bytes`` падает с
    ``AttributeError: 'int' object has no attribute 'decode'``.

    :param fetch_data: Второй элемент кортежа от ``imap.fetch`` (список частей ответа).
    :type fetch_data: Optional[list]
    :returns: Байты тела/заголовков или ``None``, если извлечь нельзя.
    :rtype: Optional[bytes]

    :example:

        >>> _imap_fetch_payload([(b'1 (BODY[HEADER] {5}', b'From:'), b')'])
        b'From:'
        >>> _imap_fetch_payload([b'1 (BODY[HEADER] NIL)']) is None
        True
    """
    if not fetch_data:
        return None
    first = fetch_data[0]
    if first is None:
        return None
    if isinstance(first, (tuple, list)) and len(first) >= 2:
        payload = first[1]
        if isinstance(payload, bytes):
            return payload
        logger.debug(
            "IMAP FETCH: payload не bytes (тип=%s), пропускаем.",
            type(payload).__name__,
        )
        return None
    logger.debug(
        "IMAP FETCH: нет literal-тела (тип первого элемента=%s), пропускаем.",
        type(first).__name__,
    )
    return None


def _find_latest_archive_attachment(
    imap: imaplib.IMAP4_SSL,
) -> Tuple[Optional[email.message.Message], Optional[str], Optional[str]]:
    """
    Ищет в настроенной IMAP-папке (:data:`config.EMAIL_IMAP_FOLDER`) одно вложение —
    архив остатков (.zip / .rar) с подходящим именем.

    Учитываются только файлы, у которых основа имени начинается с
    :data:`config.MAIL_STOCK_ARCHIVE_PREFIX` (сравнение без учёта регистра).
    Из всех таких вложений по всем письмам выбирается архив с максимальной датой
    в имени (форматы ``DD.MM.YYYY`` или ``YYYY-MM-DD`` сразу после префикса).
    Если дату из имени извлечь нельзя, для сортировки используется дата из
    заголовка ``Date`` (с предупреждением в логе); при невалидном ``Date``
    кандидат отбрасывается.

    При равной эффективной дате выбирается более новое письмо по ``Date``, затем
    больший IMAP ``msg_id``, затем лексикографически большее имя файла.

    .. note:: Технический долг (см. ``docs/refactoring-plan2.md`` §B.6): при очень
       большом числе писем в папке полный перебор ID может быть медленным. Возможные меры
       (после замеров): ограничение глубины, отдельная папка только с остатками,
       UID SEARCH по дате.

    Двухфазный обход (трафик):

    - Фаза 1: ``BODY.PEEK[HEADER]`` — отсев не-multipart.
    - Если задано :data:`config.MAIL_IMAP_MAX_MESSAGE_AGE_DAYS` (положительное ``N``),
      по заголовку ``Date`` письма при multipart: при однозначном превышении возраста
      ``N`` календарных дней обход **прерывается** без ``RFC822`` для этого и более
      старых писем. Битый ``Date`` или дата в будущем не дают раннего выхода
      (тело подгружается); см. :func:`_scan_should_stop_before_rfc822`.
    - Фаза 2: ``RFC822`` для multipart — сбор всех кандидатов с подходящим именем.

    :param imap: Авторизованный IMAP объект.
    :type imap: imaplib.IMAP4_SSL
    :returns: Кортеж (сообщение, имя архива, заголовок Date выбранного письма).
              Все ``None``, если подходящих вложений нет (результат для
              :func:`fetch_and_save` — ``NO_CHANGES``, без подмены чужим архивом).
    :rtype: Tuple[Optional[email.message.Message], Optional[str], Optional[str]]
    """
    _select_mail_folder(imap)
    folder = config.EMAIL_IMAP_FOLDER
    _, msg_ids_raw = imap.search(None, "ALL")
    msg_ids = msg_ids_raw[0].split()

    if not msg_ids:
        logger.warning("Папка IMAP %r пуста.", folder)
        return None, None, None

    logger.info(
        "Найдено писем в папке %r: %d. Сбор архивов остатков (префикс «%s»)...",
        folder,
        len(msg_ids),
        config.MAIL_STOCK_ARCHIVE_PREFIX.strip(),
    )

    candidates: List[_StockArchiveCandidate] = []
    today = date.today()
    max_msg_age = config.MAIL_IMAP_MAX_MESSAGE_AGE_DAYS

    for msg_id in reversed(msg_ids):
        # Фаза 1: загружаем только заголовки — дёшево (~2-5 КБ на письмо)
        _, header_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
        header_bytes = _imap_fetch_payload(header_data)
        if header_bytes is None:
            continue

        headers = email.message_from_bytes(header_bytes)
        content_type = headers.get("Content-Type", "").lower()

        # Вложения возможны только в multipart-письмах
        if "multipart" not in content_type:
            continue

        date_str = headers.get("Date", "")
        stop_scan, date_unparsed = _scan_should_stop_before_rfc822(
            date_str, max_msg_age, today
        )
        if date_unparsed and max_msg_age is not None:
            logger.warning(
                "Заголовок Date не разобран, ранний выход по окну (%s дн.) не "
                "применяется; подгружаем RFC822 (письмо id=%s).",
                max_msg_age,
                msg_id.decode("ascii", errors="replace"),
            )
        if stop_scan:
            logger.info(
                "Окно по дате письма (%s дн.): письмо id=%s старше окна (Date=%s), "
                "обход остановлен.",
                max_msg_age,
                msg_id.decode("ascii", errors="replace"),
                date_str,
            )
            break

        # Фаза 2: письмо многочастное — загружаем полностью
        _, msg_data = imap.fetch(msg_id, "(RFC822)")
        msg_bytes = _imap_fetch_payload(msg_data)
        if msg_bytes is None:
            continue

        msg = email.message_from_bytes(msg_bytes)
        date_str = msg.get("Date", "") or date_str
        email_dt = _parse_email_date_for_sort(date_str)

        for part in msg.walk():
            content_disposition = part.get("Content-Disposition", "")
            if "attachment" not in content_disposition:
                continue

            raw_filename = part.get_filename()
            if not raw_filename:
                continue

            filename = _decode_mime_words(raw_filename)
            _stem, ext = _stem_and_ext(filename)
            if not _is_archive_extension(ext):
                continue
            if not _filename_has_stock_prefix(filename):
                logger.info(
                    "Архив отброшен (имя не по префиксу «%s»): %s",
                    config.MAIL_STOCK_ARCHIVE_PREFIX.strip(),
                    filename,
                )
                continue

            eff = _effective_stock_date(filename, date_str)
            if eff is None:
                continue
            stock_date, parsed_fn = eff
            candidates.append(
                _StockArchiveCandidate(
                    msg=msg,
                    filename=filename,
                    email_date=date_str,
                    msg_id=msg_id,
                    stock_date=stock_date,
                    parsed_filename_date=parsed_fn,
                    email_dt=email_dt,
                )
            )
            src = "имя" if parsed_fn is not None else "письмо"
            logger.info(
                "Кандидат архива остатков: %s, эффективная дата %s (%s), письмо Date=%s",
                filename,
                stock_date.isoformat(),
                src,
                date_str,
            )

    if not candidates:
        logger.warning(
            "Подходящих архивов остатков не найдено (префикс «%s», только .zip/.rar).",
            config.MAIL_STOCK_ARCHIVE_PREFIX.strip(),
        )
        return None, None, None

    best = _pick_best_stock_candidate(candidates)
    if best is None:
        return None, None, None
    parsed_label = (
        best.parsed_filename_date.isoformat()
        if best.parsed_filename_date is not None
        else "fallback: дата письма"
    )
    logger.info(
        "Выбран архив остатков: %s (эффективная дата %s; разбор имени: %s). Всего кандидатов: %d",
        best.filename,
        best.stock_date.isoformat(),
        parsed_label,
        len(candidates),
    )
    return best.msg, best.filename, best.email_date


def _download_archive(
    msg: email.message.Message, archive_filename: str
) -> Optional[str]:
    """
    Скачивает вложение-архив из письма во временную папку temp/.

    :param msg: Объект email-сообщения с вложением.
    :type msg: email.message.Message
    :param archive_filename: Имя файла архива для сохранения.
    :type archive_filename: str
    :returns: Абсолютный путь к сохранённому архиву или None при ошибке.
    :rtype: Optional[str]
    """
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    save_path = os.path.join(config.TEMP_DIR, archive_filename)

    for part in msg.walk():
        raw_filename = part.get_filename()
        if not raw_filename:
            continue

        decoded_name = _decode_mime_words(raw_filename)
        if decoded_name != archive_filename:
            continue

        payload = part.get_payload(decode=True)
        if payload:
            with open(save_path, "wb") as f:
                f.write(payload)
            logger.info("Архив сохранён: %s (%d байт)", save_path, len(payload))
            return save_path

    logger.error("Не удалось извлечь вложение '%s' из письма.", archive_filename)
    return None


def _extract_xlsx_from_archive(archive_path: str) -> Optional[str]:
    """
    Извлекает первый найденный .xlsx файл из архива (.zip или .rar) в папку temp/.

    :param archive_path: Путь к архиву на диске.
    :type archive_path: str
    :returns: Путь к извлечённому .xlsx файлу или None если файл не найден.
    :rtype: Optional[str]
    :raises RuntimeError: Если формат архива .rar и rarfile не установлен.
    :raises zipfile.BadZipFile: Если zip-архив повреждён.
    """
    extract_dir = config.TEMP_DIR

    if archive_path.lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xlsx"):
                    zf.extract(name, extract_dir)
                    # normpath нормализует разделители пути: ZIP-стандарт
                    # использует "/" внутри имён, на Windows это может
                    # привести к смешанным разделителям без нормализации.
                    extracted_path = os.path.normpath(
                        os.path.join(extract_dir, name)
                    )
                    logger.info("Извлечён xlsx из zip: %s", extracted_path)
                    return extracted_path

    elif archive_path.lower().endswith(".rar"):
        if not RAR_SUPPORT:
            raise RuntimeError(
                "Архив имеет формат .rar, но модуль rarfile не установлен. "
                "Выполните: pip install rarfile"
            )
        with rarfile.RarFile(archive_path, "r") as rf:
            for name in rf.namelist():
                if name.lower().endswith(".xlsx"):
                    rf.extract(name, extract_dir)
                    extracted_path = os.path.normpath(
                        os.path.join(extract_dir, name)
                    )
                    logger.info("Извлечён xlsx из rar: %s", extracted_path)
                    return extracted_path

    logger.error("Файл .xlsx не найден внутри архива: %s", archive_path)
    return None


def _save_meta(archive_filename: str, email_date: str) -> None:
    """
    Сохраняет метаданные последнего обновления в storage/meta.json.

    Включает поле last_check_date с сегодняшней датой, чтобы после полного
    обновления дата проверки не терялась (cleanup_old_data удаляет meta.json).

    :param archive_filename: Имя архива из письма.
    :type archive_filename: str
    :param email_date: Строка с датой письма из заголовка Date.
    :type email_date: str
    :returns: None
    """
    from storage.data_store import atomic_write_json

    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    meta = {
        "archive_filename": archive_filename,
        "email_date": email_date,
        "updated_at": datetime.now().isoformat(),
        "last_check_date": datetime.now().strftime("%Y-%m-%d"),
    }
    atomic_write_json(config.META_JSON_PATH, meta, ensure_ascii=False, indent=2)
    logger.info("Метаданные сохранены: %s", config.META_JSON_PATH)


def load_meta() -> Optional[dict]:
    """
    Читает метаданные последнего обновления из storage/meta.json.

    Делегирует :func:`storage.data_store.read_meta_json` — единая точка чтения
    (для скриптов и отладки; в боте статус строится через ``data_store``).

    :returns: Словарь с метаданными или None если файл не существует
              или повреждён.
    :rtype: Optional[dict]
    """
    from storage.data_store import read_meta_json

    return read_meta_json()


def fetch_and_save(force: bool = False) -> MailFetchOutcome:
    """
    Главная функция модуля: скачивает последние остатки с почты и сохраняет xlsx.

    Возвращает :class:`MailFetchOutcome` (явная модель вместо ``Optional[bool]``):
    - ``UPDATED`` — данные успешно обновлены (новое письмо скачано и сохранено)
    - ``NO_CHANGES`` — IMAP проверен, нет архива с префиксом остатков в имени, либо
      дата выбранного письма не новее сохранённой в ``meta.json``
    - ``SKIPPED_TODAY`` — IMAP не вызывался, т.к. сегодня уже была проверка (``force=False``)
    - ``ARCHIVE_FAILURE`` — не удалось скачать вложение или извлечь .xlsx из архива

    Логика ветвления (упрощённо):
    1. Если ``not force`` и сегодня уже была проверка и есть ``current.xlsx`` —
       возвращает ``SKIPPED_TODAY`` (IMAP не вызывается).
    2. Подключение к IMAP, поиск последнего письма с архивом.
    3. Писем с архивом нет — ``set_last_check_date(today)``, возврат ``NO_CHANGES``.
    4. Письмо есть, но дата не новее сохранённой в ``meta.json`` —
       ``set_last_check_date(today)``, возврат ``NO_CHANGES``.
    5. Скачивание вложения и извлечение ``.xlsx`` — при сбое возврат
       ``ARCHIVE_FAILURE`` (``last_check_date`` не обновляется).
    6. Успех: ``cleanup_old_data``, копирование в ``current.xlsx``, ``_save_meta``,
       возврат ``UPDATED``. В ``finally`` — закрытие IMAP и очистка ``temp/``.

    Важно: ``last_check_date`` записывается при успешном завершении веток без
    сбоя архива или при подтверждённой актуальности (шаги 3–4). При ошибке
    загрузки/распаковки архива ``last_check_date`` не обновляется, чтобы
    следующий запрос снова обратился к IMAP.

    :param force: Если True — игнорирует last_check_date, всегда идёт на IMAP.
    :type force: bool
    :returns: Элемент :class:`MailFetchOutcome`.
    :rtype: MailFetchOutcome
    :raises Exception: При критических ошибках подключения или работы с файлами.

    :example:
    >>> result = fetch_and_save()
    >>> if result == MailFetchOutcome.SKIPPED_TODAY:
    ...     print("Сегодня уже проверялось, IMAP пропущен")
    >>> elif result == MailFetchOutcome.NO_CHANGES:
    ...     print("Проверили почту, новых писем нет")
    >>> elif result == MailFetchOutcome.ARCHIVE_FAILURE:
    ...     print("Сбой архива — см. логи MAIL_FETCH_ARCHIVE_FAILURE")
    >>> else:
    ...     print("Данные обновлены")
    """
    from datetime import date as _date
    from storage.data_store import (
        check_update_available,
        cleanup_old_data,
        get_last_check_date,
        set_last_check_date,
    )

    today = _date.today().isoformat()

    # Защита от частых запросов к почте.
    # Пропускаем IMAP только если сегодня уже проверяли И файл с данными существует.
    # Если current.xlsx отсутствует — идём на почту в любом случае.
    if not force and get_last_check_date() == today and os.path.exists(config.CURRENT_XLSX_PATH):
        logger.info("Почта уже проверялась сегодня (%s). IMAP пропущен.", today)
        return MailFetchOutcome.SKIPPED_TODAY

    imap = None
    try:
        imap = _connect_imap()

        msg, archive_filename, email_date = _find_latest_archive_attachment(imap)

        if msg is None:
            # Писем нет — фиксируем дату проверки, повторная проверка сегодня бессмысленна
            set_last_check_date(today)
            logger.warning("Подходящих писем не найдено. Завершение.")
            return MailFetchOutcome.NO_CHANGES

        # Проверяем: уже скачивали это письмо?
        # check_update_available сравнивает datetime с учётом timezone — защита от
        # ситуации когда одно письмо возвращается сервером с разными TZ-смещениями
        # (например, "+0000" vs "+0800" для того же момента времени).
        if not force and not check_update_available(email_date):
            # Данные актуальны — фиксируем дату проверки
            set_last_check_date(today)
            return MailFetchOutcome.NO_CHANGES

        archive_path = _download_archive(msg, archive_filename)
        if archive_path is None:
            logger.error(
                "MAIL_FETCH_ARCHIVE_FAILURE: не удалось скачать вложение из письма "
                "(вложение: %s).",
                archive_filename,
            )
            return MailFetchOutcome.ARCHIVE_FAILURE

        try:
            xlsx_path = _extract_xlsx_from_archive(archive_path)
        except Exception as ex:
            logger.error(
                "MAIL_FETCH_ARCHIVE_FAILURE: ошибка извлечения .xlsx из архива %s: %s",
                archive_path,
                ex,
                exc_info=True,
            )
            return MailFetchOutcome.ARCHIVE_FAILURE

        if xlsx_path is None:
            logger.error(
                "MAIL_FETCH_ARCHIVE_FAILURE: .xlsx не найден в архиве после скачивания: %s",
                archive_path,
            )
            return MailFetchOutcome.ARCHIVE_FAILURE

        # Удаляем старые данные перед сохранением новых
        cleanup_old_data()

        os.makedirs(config.STORAGE_DIR, exist_ok=True)
        shutil.copy2(xlsx_path, config.CURRENT_XLSX_PATH)
        logger.info("Актуальный xlsx сохранён: %s", config.CURRENT_XLSX_PATH)

        _save_meta(archive_filename, email_date)
        return MailFetchOutcome.UPDATED

    except Exception as e:
        logger.error("Ошибка при получении остатков с почты: %s", e, exc_info=True)
        raise

    finally:
        if imap:
            try:
                imap.logout()
                logger.info("IMAP соединение закрыто.")
            except Exception:
                pass
        _cleanup_temp()


def _cleanup_temp() -> None:
    """
    Удаляет все файлы из временной папки temp/ после успешной обработки.

    :returns: None
    """
    if not os.path.exists(config.TEMP_DIR):
        return
    for filename in os.listdir(config.TEMP_DIR):
        file_path = os.path.join(config.TEMP_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            logger.warning("Не удалось удалить временный файл %s: %s", file_path, e)
    logger.info("Временная папка temp/ очищена.")
