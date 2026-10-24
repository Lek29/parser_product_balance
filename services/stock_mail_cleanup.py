"""
Очистка IMAP-папки с остатками: один keeper + удаление лишних/старых писем.

Правила (календарная неделя Пн–Вс, Europe/Moscow):

* keeper — лучший кандидат по тем же правилам, что бот
  (:func:`parser.mail_fetcher._pick_best_stock_candidate`);
* удаляются все прочие письма с архивом остатков;
* удаляются письма с ``Date`` строго до понедельника текущей недели
  (keeper никогда не удаляется).

Вызывается из CLI ``scripts/cleanup_stock_mail.py`` и еженедельного
задачи планировщика.
"""

from __future__ import annotations

import email
import logging
from datetime import date
from typing import Optional

import config

logger = logging.getLogger(__name__)


def _msg_id_label(msg_id: bytes) -> str:
    """
    Человекочитаемый IMAP id для логов.

    :param msg_id: Сырой идентификатор сообщения.
    :type msg_id: bytes
    :returns: ASCII-строка id.
    :rtype: str
    """
    return msg_id.decode("ascii", errors="replace")


def _header_date(imap, msg_id: bytes) -> tuple[Optional[date], str]:
    """
    Читает календарную дату из заголовка ``Date`` письма.

    :param imap: Авторизованное IMAP-соединение.
    :param msg_id: Идентификатор сообщения.
    :type msg_id: bytes
    :returns: ``(date|None, сырая строка Date)``.
    :rtype: tuple[Optional[date], str]
    """
    from parser.mail_fetcher import (
        _imap_fetch_payload,
        _parse_email_date_for_sort,
    )

    _, header_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
    header_bytes = _imap_fetch_payload(header_data)
    if header_bytes is None:
        return None, ""
    headers = email.message_from_bytes(header_bytes)
    date_str = headers.get("Date", "") or ""
    dt = _parse_email_date_for_sort(date_str)
    if dt.year <= 1:
        return None, date_str
    return dt.date(), date_str


def plan_deletions(imap) -> tuple[Optional[bytes], set[bytes], list[str]]:
    """
    Строит план удаления: keeper и множество msg_id к удалению.

    :param imap: Авторизованное IMAP-соединение (папка выбирается при сборе).
    :returns: ``(keeper_id|None, ids_to_delete, human lines for log)``.
    :rtype: tuple[Optional[bytes], set[bytes], list[str]]
    """
    from parser.mail_fetcher import (
        _pick_best_stock_candidate,
        _select_mail_folder,
        collect_stock_archive_candidates,
    )
    from services.calendar_week import today_moscow, week_monday

    candidates = collect_stock_archive_candidates(imap, apply_age_window=False)
    best = _pick_best_stock_candidate(candidates) if candidates else None
    keeper_id: Optional[bytes] = best.msg_id if best is not None else None

    lines: list[str] = []
    if best is not None:
        lines.append(
            f"KEEPER id={_msg_id_label(best.msg_id)} file={best.filename!r} "
            f"stock_date={best.stock_date.isoformat()}"
        )
    else:
        lines.append("KEEPER: нет кандидатов с архивом остатков")

    to_delete: set[bytes] = set()
    for c in candidates:
        if keeper_id is not None and c.msg_id == keeper_id:
            continue
        to_delete.add(c.msg_id)
        lines.append(f"DELETE (лишний архив остатков) id={_msg_id_label(c.msg_id)} " f"file={c.filename!r}")

    _select_mail_folder(imap)
    _, msg_ids_raw = imap.search(None, "ALL")
    all_ids = msg_ids_raw[0].split() if msg_ids_raw and msg_ids_raw[0] else []
    week_start = week_monday(today_moscow())
    lines.append(f"Неделя МСК с {week_start.isoformat()} (Пн)")

    for msg_id in all_ids:
        if keeper_id is not None and msg_id == keeper_id:
            continue
        if msg_id in to_delete:
            continue
        msg_day, raw = _header_date(imap, msg_id)
        if msg_day is None:
            continue
        if msg_day < week_start:
            to_delete.add(msg_id)
            lines.append(
                f"DELETE (старше недели) id={_msg_id_label(msg_id)} " f"Date={raw!r} day={msg_day.isoformat()}"
            )

    lines.append(
        f"Итого к удалению: {len(to_delete)}; папка={config.EMAIL_IMAP_FOLDER!r}; " f"писем в папке: {len(all_ids)}"
    )
    return keeper_id, to_delete, lines


def apply_deletions(imap, msg_ids: set[bytes]) -> int:
    """
    Помечает письма ``\\Deleted`` и делает ``EXPUNGE``.

    :param imap: Авторизованное IMAP-соединение с открытой папкой.
    :param msg_ids: Идентификаторы к удалению.
    :type msg_ids: set[bytes]
    :returns: Число успешно помеченных писем.
    :rtype: int
    """
    marked = 0
    for msg_id in sorted(msg_ids, key=lambda b: int(b) if b.isdigit() else b):
        typ, _ = imap.store(msg_id, "+FLAGS", "\\Deleted")
        if typ != "OK":
            logger.error(
                "STORE \\Deleted failed for id=%s typ=%s",
                _msg_id_label(msg_id),
                typ,
            )
            continue
        marked += 1
        logger.info("Помечено \\Deleted: id=%s", _msg_id_label(msg_id))
    if marked:
        typ, _ = imap.expunge()
        logger.info("EXPUNGE: typ=%s, помечено=%d", typ, marked)
    return marked


def run_stock_mail_cleanup(*, execute: bool = False) -> int:
    """
    Полный цикл очистки папки :data:`config.EMAIL_IMAP_FOLDER`.

    :param execute: Если ``False`` — только план (dry-run); если ``True`` —
        ``STORE \\Deleted`` + ``EXPUNGE``.
    :type execute: bool
    :returns: Число удалённых (помеченных) писем; ``0`` при dry-run или пустом плане.
    :rtype: int
    :raises Exception: Пробрасывает ошибки IMAP/сети наружу (caller логирует).
    :side effects: При ``execute=True`` удаляет письма на сервере.
    """
    from parser.mail_fetcher import _connect_imap

    imap = None
    try:
        imap = _connect_imap()
        keeper_id, to_delete, lines = plan_deletions(imap)
        for line in lines:
            logger.info("%s", line)

        if not execute:
            logger.info("DRY-RUN: ничего не удалено (execute=False).")
            return 0

        if not to_delete:
            logger.info("Нечего удалять.")
            return 0

        n = apply_deletions(imap, to_delete)
        logger.info(
            "Готово. Удалено (помечено): %d. Keeper: %s",
            n,
            _msg_id_label(keeper_id) if keeper_id else "—",
        )
        return n
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                logger.debug("IMAP logout при cleanup завершился с ошибкой", exc_info=True)
