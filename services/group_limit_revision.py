"""
Еженедельная ревизия порогов по группам (``docs/fourty_role.md``, этап 7а).

Отдельная задача планировщика: при наличии лимитов в хранилище получает
актуальный снимок остатков (локальный при критерии свежести из этапа 0 §4
или цепочка почты через :func:`parser.mail_fetcher.fetch_and_save`),
затем проверка лимитов только по группам из ``list_all_limits()`` и отправка
алерта при нарушениях.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Final, Literal, Optional
from zoneinfo import ZoneInfo

import config
from parser.excel_parser import parse_stock
from parser.mail_fetcher import MailFetchOutcome, fetch_and_save
from services.stock_pipeline import (
    run_parse_and_save_filtered,
    send_group_limit_alerts_for_items,
)
from storage.data_store import (
    get_last_check_date,
    list_all_limits,
    read_meta_json,
)

logger = logging.getLogger(__name__)

MOSCOW_TZ: Final = ZoneInfo("Europe/Moscow")
REVISION_MAX_META_AGE_HOURS: Final = 168

DataRevisionSource = Literal["used_local_snapshot", "fetched_from_mail"]


def _reference_moscow(reference: Optional[datetime]) -> datetime:
    """
    Приводит момент времени к таймзоне Europe/Moscow.

    :param reference: Момент или ``None`` (тогда «сейчас» по Москве).
    :type reference: Optional[datetime]
    :returns: Aware-datetime в Europe/Moscow.
    :rtype: datetime
    """
    if reference is None:
        return datetime.now(MOSCOW_TZ)
    if reference.tzinfo is None:
        return reference.replace(tzinfo=MOSCOW_TZ)
    return reference.astimezone(MOSCOW_TZ)


def local_snapshot_acceptable_for_limit_revision(
    *,
    reference: Optional[datetime] = None,
) -> bool:
    """
    Проверяет, можно ли для ревизии лимитов обойтись без ``fetch_and_save``.

    Критерий из контракта этапа 0 §4: файл ``current.xlsx`` существует,
    :func:`parser.excel_parser.parse_stock` по нему успешен, и выполняется
    хотя бы одно из условий:

    * **(а)** ``last_check_date`` в ``meta.json`` совпадает с календарной
      датой ``reference`` по Москве;
    * **(б)** поле ``updated_at`` (ISO) не старше 168 ч от ``reference``,
      сравнение после приведения к Europe/Moscow (naive ISO трактуется как МСК).

    :param reference: Точка отсчёта «сейчас» (для тестов); по умолчанию Москва.
    :type reference: Optional[datetime]
    :returns: ``True``, если локальный снимок допустим без новой загрузки
              с почты.
    :rtype: bool
    """
    ref = _reference_moscow(reference)
    path = config.CURRENT_XLSX_PATH
    if not os.path.isfile(path):
        logger.info("Ревизия лимитов: локальный снимок недоступен — нет файла.")
        return False
    try:
        parse_stock(path)
    except Exception:
        logger.warning(
            "Ревизия лимитов: parse_stock(current.xlsx) не удался — " "локальный снимок не принят.",
            exc_info=True,
        )
        return False

    today_msk = ref.date().isoformat()
    if get_last_check_date() == today_msk:
        logger.info(
            "Ревизия лимитов: локальный снимок OK (last_check_date == сегодня МСК).",
        )
        return True

    meta = read_meta_json() or {}
    raw = meta.get("updated_at")
    if not raw or not isinstance(raw, str):
        logger.info(
            "Ревизия лимитов: нет updated_at в meta — локальный снимок не принят.",
        )
        return False
    try:
        u = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.info("Ревизия лимитов: updated_at в meta не разобран.")
        return False
    if u.tzinfo is None:
        u_msk = u.replace(tzinfo=MOSCOW_TZ)
    else:
        u_msk = u.astimezone(MOSCOW_TZ)
    if u_msk >= ref - timedelta(hours=REVISION_MAX_META_AGE_HOURS):
        logger.info(
            "Ревизия лимитов: локальный снимок OK (updated_at не старше %d ч).",
            REVISION_MAX_META_AGE_HOURS,
        )
        return True

    logger.info(
        "Ревизия лимитов: локальный снимок не свежий (ни дата проверки, " "ни updated_at в окне %d ч).",
        REVISION_MAX_META_AGE_HOURS,
    )
    return False


def run_weekly_group_limit_revision() -> None:
    """
    Точка входа задачи планировщика «ревизия порогов по группам».

    Если ``list_all_limits()`` пуст — выход без действий. Иначе получает
    снимок (локально или через ``fetch_and_save``), затем одну проверку
    лимитов и при необходимости отправку (см. этапы 3 и 5–6 в плане роли).

    При ``MailFetchOutcome.UPDATED`` вызывается
    ``run_parse_and_save_filtered`` (полный пайплайн и один алерт).
    Иначе парсит ``current.xlsx`` и вызывает
    ``send_group_limit_alerts_for_items``.

    Исключения не пробрасывает наружу — только лог, чтобы не ронять бота.

    :returns: None
    :rtype: None
    """
    try:
        limits = list_all_limits()
        if not limits:
            logger.info(
                "Ревизия порогов по группам: нет лимитов — задача пропущена.",
            )
            return

        if local_snapshot_acceptable_for_limit_revision():
            source: DataRevisionSource = "used_local_snapshot"
            logger.info(
                "Ревизия порогов по группам: источник=%s, групп_с_лимитом=%d.",
                source,
                len(limits),
            )
            items = parse_stock(config.CURRENT_XLSX_PATH)
            send_group_limit_alerts_for_items(
                items,
                log_context="weekly_revision",
            )
            return

        source = "fetched_from_mail"
        logger.info(
            "Ревизия порогов по группам: источник=%s (fetch_and_save), " "групп_с_лимитом=%d.",
            source,
            len(limits),
        )
        outcome = fetch_and_save()

        if outcome == MailFetchOutcome.ARCHIVE_FAILURE:
            logger.error(
                "Ревизия порогов: fetch_and_save → ARCHIVE_FAILURE, выход.",
            )
            return

        if outcome == MailFetchOutcome.UPDATED:
            run_parse_and_save_filtered()
            return

        if not os.path.isfile(config.CURRENT_XLSX_PATH):
            logger.error(
                "Ревизия порогов: после fetch нет %s — парсинг невозможен.",
                config.CURRENT_XLSX_PATH,
            )
            return

        try:
            items = parse_stock(config.CURRENT_XLSX_PATH)
        except Exception:
            logger.error(
                "Ревизия порогов: parse_stock после fetch — ошибка.",
                exc_info=True,
            )
            return

        send_group_limit_alerts_for_items(
            items,
            log_context="weekly_revision",
        )

    except Exception:
        logger.error(
            "Ревизия порогов по группам: необработанное исключение.",
            exc_info=True,
        )
