"""
Модуль получения Excel-остатков с корпоративной почты.

Подключается к почтовому серверу Яндекс по IMAP SSL, ищет последнее письмо
с вложением-архивом (.zip или .rar), скачивает его в папку temp/,
распаковывает и сохраняет найденный .xlsx файл в storage/current.xlsx.
Метаданные (дата письма, имя файла) сохраняются в storage/meta.json.
"""

import email
import email.message
import imaplib
import json
import logging
import os
import shutil
import zipfile
from datetime import datetime
from email.header import decode_header
from typing import Optional, Tuple

import config

logger = logging.getLogger(__name__)

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


def _find_latest_archive_attachment(
    imap: imaplib.IMAP4_SSL,
) -> Tuple[Optional[email.message.Message], Optional[str], Optional[str]]:
    """
    Ищет в папке INBOX последнее письмо с вложением-архивом (.zip или .rar).

    Использует двухфазный поиск для минимизации трафика при большом inbox:

    - Фаза 1: загружает только заголовки каждого письма (BODY.PEEK[HEADER],
      ~2-5 КБ). Письма без Content-Type: multipart/* пропускаются мгновенно —
      вложений в них быть не может.
    - Фаза 2: только для multipart-писем загружает полный RFC822 и проверяет
      наличие .zip/.rar вложения.

    :param imap: Авторизованный IMAP объект.
    :type imap: imaplib.IMAP4_SSL
    :returns: Кортеж (email-сообщение, имя файла вложения, дата письма).
              Все элементы None если подходящее письмо не найдено.
    :rtype: Tuple[Optional[email.message.Message], Optional[str], Optional[str]]
    """
    imap.select("INBOX")
    _, msg_ids_raw = imap.search(None, "ALL")
    msg_ids = msg_ids_raw[0].split()

    if not msg_ids:
        logger.warning("Папка INBOX пуста.")
        return None, None, None

    logger.info(
        "Найдено писем в INBOX: %d. Ищем последнее с архивом...",
        len(msg_ids),
    )

    for msg_id in reversed(msg_ids):
        # Фаза 1: загружаем только заголовки — дёшево (~2-5 КБ на письмо)
        _, header_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
        if not header_data or not header_data[0]:
            continue

        headers = email.message_from_bytes(header_data[0][1])
        content_type = headers.get("Content-Type", "").lower()

        # Вложения возможны только в multipart-письмах
        if "multipart" not in content_type:
            continue

        # Фаза 2: письмо многочастное — загружаем полностью
        _, msg_data = imap.fetch(msg_id, "(RFC822)")
        if not msg_data or not msg_data[0]:
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        date_str = msg.get("Date", "")

        for part in msg.walk():
            content_disposition = part.get("Content-Disposition", "")
            if "attachment" not in content_disposition:
                continue

            raw_filename = part.get_filename()
            if not raw_filename:
                continue

            filename = _decode_mime_words(raw_filename)

            if filename.lower().endswith(".zip") or filename.lower().endswith(".rar"):
                logger.info(
                    "Найдено письмо с архивом: '%s' от %s",
                    filename, date_str,
                )
                return msg, filename, date_str

    logger.warning("Писем с вложением-архивом не найдено.")
    return None, None, None


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
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    meta = {
        "archive_filename": archive_filename,
        "email_date": email_date,
        "updated_at": datetime.now().isoformat(),
        "last_check_date": datetime.now().strftime("%Y-%m-%d"),
    }
    with open(config.META_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info("Метаданные сохранены: %s", config.META_JSON_PATH)


def load_meta() -> Optional[dict]:
    """
    Читает метаданные последнего обновления из storage/meta.json.

    :returns: Словарь с метаданными или None если файл не существует
              или повреждён.
    :rtype: Optional[dict]
    """
    if not os.path.exists(config.META_JSON_PATH):
        return None
    try:
        with open(config.META_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error("meta.json повреждён (load_meta): %s", e)
        return None


def fetch_and_save(force: bool = False) -> Optional[bool]:
    """
    Главная функция модуля: скачивает последние остатки с почты и сохраняет xlsx.

    Возвращает три различимых результата:
    - True  — данные успешно обновлены (новое письмо скачано и сохранено)
    - False — IMAP проверен, но нового письма нет (данные уже актуальны)
    - None  — IMAP не вызывался, т.к. сегодня уже была проверка (force=False)

    Полный цикл при наличии нового письма:
    1. Если not force: проверяет last_check_date — если сегодня, возвращает None.
    2. Подключается к IMAP.
    3. Ищет последнее письмо с архивом.
    4. Если писем нет — записывает last_check_date = сегодня, возвращает False.
    5. Сравнивает дату письма с сохранённой — если не новое, записывает
       last_check_date = сегодня, возвращает False.
    6. Скачивает архив, извлекает xlsx.
    7. Удаляет старые файлы (cleanup_old_data), сохраняет current.xlsx.
    8. Сохраняет meta.json (включает last_check_date).
    9. Очищает temp/.

    Важно: last_check_date записывается ТОЛЬКО при успешном завершении или при
    подтверждённой актуальности данных (шаги 4, 5, 8). При ошибке загрузки/
    распаковки last_check_date НЕ обновляется, чтобы следующий запрос снова
    попытался обратиться к IMAP.

    :param force: Если True — игнорирует last_check_date, всегда идёт на IMAP.
    :type force: bool
    :returns: True / False / None (см. описание выше).
    :rtype: Optional[bool]
    :raises Exception: При критических ошибках подключения или работы с файлами.

    :example:
    >>> result = fetch_and_save()
    >>> if result is None:
    ...     print("Сегодня уже проверялось, IMAP пропущен")
    >>> elif result is False:
    ...     print("Проверили почту, новых писем нет")
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
        return None

    imap = None
    try:
        imap = _connect_imap()

        msg, archive_filename, email_date = _find_latest_archive_attachment(imap)

        if msg is None:
            # Писем нет — фиксируем дату проверки, повторная проверка сегодня бессмысленна
            set_last_check_date(today)
            logger.warning("Подходящих писем не найдено. Завершение.")
            return False

        # Проверяем: уже скачивали это письмо?
        # check_update_available сравнивает datetime с учётом timezone — защита от
        # ситуации когда одно письмо возвращается сервером с разными TZ-смещениями
        # (например, "+0000" vs "+0800" для того же момента времени).
        if not force and not check_update_available(email_date):
            # Данные актуальны — фиксируем дату проверки
            set_last_check_date(today)
            return False

        archive_path = _download_archive(msg, archive_filename)
        if archive_path is None:
            return False

        xlsx_path = _extract_xlsx_from_archive(archive_path)
        if xlsx_path is None:
            return False

        # Удаляем старые данные перед сохранением новых
        cleanup_old_data()

        os.makedirs(config.STORAGE_DIR, exist_ok=True)
        shutil.copy2(xlsx_path, config.CURRENT_XLSX_PATH)
        logger.info("Актуальный xlsx сохранён: %s", config.CURRENT_XLSX_PATH)

        _save_meta(archive_filename, email_date)
        return True

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
