"""
Пайплайн обработки остатков после успешной загрузки current.xlsx.

Хэндлер актуализации и планировщик вызывают одну функцию — без дублирования.
"""

from __future__ import annotations

import logging
import os

import config
from bot.telegram_bridge import send_group_limit_alert_html
from parser.excel_parser import (
    filter_by_threshold,
    find_group_limit_violations,
    generate_filtered_xlsx,
    normalize_top_category_label,
    parse_stock,
)
from services.group_limit_alert_text import format_group_limit_alerts_html
from storage.data_store import (
    current_xlsx_fingerprint,
    data_json_matches_current_xlsx,
    list_all_limits,
    load_items,
    save_items,
)

logger = logging.getLogger(__name__)


def _monitored_group_keys(limits: dict[str, int]) -> list[str]:
    """
    Ключи групп под надзором в том же порядке нормализации, что и при проверке.

    :param limits: Сырой словарь из хранилища лимитов.
    :type limits: dict[str, int]
    :returns: Отсортированный список уникальных нормализованных имён групп.
    :rtype: list[str]
    """
    seen: set[str] = set()
    keys: list[str] = []
    for raw_key, raw_limit in limits.items():
        gk = normalize_top_category_label(str(raw_key))
        try:
            if int(raw_limit) < 0:
                continue
        except (TypeError, ValueError):
            continue
        if gk not in seen:
            seen.add(gk)
            keys.append(gk)
    keys.sort(key=lambda s: s.casefold())
    return keys


def send_group_limit_alerts_for_items(
    all_items: list[dict],
    *,
    log_context: str = "after_parse",
) -> None:
    """
    Проверяет лимиты по группам для уже распарсенного снимка и шлёт один алерт.

    Используется полным пайплайном :func:`run_parse_and_save_filtered` и
    еженедельной ревизией (``services.group_limit_revision``), когда данные
    уже загружены в память без повторного парсинга.

    Логирует сводку (этап 8 ``docs/fourty_role.md``): какие группы под проверкой,
    в каких есть нарушения, сколько позиций попало в сообщение. Ошибки при расчёте
    HTML или отправке перехватываются и не пробрасываются наружу.

    :param all_items: Результат :func:`parser.excel_parser.parse_stock`.
    :type all_items: list[dict]
    :param log_context: Метка сценария в логах (например ``after_parse``).
    :type log_context: str
    :returns: None
    :rtype: None
    :side effects: При ненулевых нарушениях — попытка отправки в Telegram.
    """
    limits = list_all_limits()
    if not limits:
        return

    groups_checked = _monitored_group_keys(limits)
    if not groups_checked:
        logger.info(
            "group_limit_check context=%s phase=skip reason=no_valid_limits",
            log_context,
        )
        return

    try:
        violations = find_group_limit_violations(all_items, limits)
        alert_html = format_group_limit_alerts_html(violations)
    except Exception:
        logger.error(
            "group_limit_check context=%s phase=build result=fail",
            log_context,
            exc_info=True,
        )
        return

    triggered = [str(v.get("group", "")) for v in violations]
    positions_in_alert = sum(len(v.get("positions") or []) for v in violations)
    sep = "|"
    logger.info(
        "group_limit_check context=%s phase=eval groups_checked=%s "
        "n_checked=%d groups_triggered=%s n_triggered=%d "
        "positions_in_alert=%d",
        log_context,
        sep.join(groups_checked),
        len(groups_checked),
        sep.join(triggered) if triggered else "-",
        len(triggered),
        positions_in_alert,
    )

    if not alert_html:
        logger.info(
            "group_limit_check context=%s phase=send result=skipped reason=no_message",
            log_context,
        )
        return

    logger.info(
        "group_limit_check context=%s phase=format chars=%d",
        log_context,
        len(alert_html),
    )
    try:
        send_group_limit_alert_html(alert_html)
    except Exception:
        logger.error(
            "group_limit_check context=%s phase=send result=unexpected_error",
            log_context,
            exc_info=True,
        )


def run_parse_and_save_filtered() -> tuple[int, int]:
    """
    Парсит ``storage/current.xlsx``, пишет ``data.json`` и глобальный ``filtered.xlsx``.

    Файл остатков уже должен быть на месте (после успешного
    :func:`parser.mail_fetcher.fetch_and_save`).

    Если ``data.json`` уже соответствует отпечатку текущего ``current.xlsx``
    (mtime+size), повторный ``parse_stock`` не выполняется: берутся items из
    JSON; ``filtered.xlsx`` пересобирается только если файла нет.

    Порядок при полном прогоне: сначала полный снимок в JSON, затем
    фильтрация и запись ``filtered.xlsx``, чтобы при сбое генерации Excel
    в ``data.json`` уже лежал актуальный полный список (удобно для отладки).

    :returns: Кортеж ``(число всех позиций, число позиций с малым остатком)``.
    :rtype: tuple[int, int]
    :side effects: Обновляет ``data.json`` и ``filtered.xlsx`` в ``storage/``.
                   Лимиты по группам — через
                   :func:`send_group_limit_alerts_for_items` (только при полном парсе).
                   Zero-sold push — :func:`services.zero_sold.run_zero_sold_check_after_parse`
                   только при полном парсе (не на cache-hit).
    """
    if data_json_matches_current_xlsx():
        all_items = load_items()
        filtered = filter_by_threshold(all_items)
        if not os.path.isfile(config.FILTERED_XLSX_PATH):
            generate_filtered_xlsx(filtered)
            logger.info(
                "Кэш парсинга: data.json совпал с current.xlsx; " "пересобран отсутствующий filtered.xlsx (%d поз.).",
                len(filtered),
            )
        else:
            logger.info(
                "Кэш парсинга: data.json совпал с current.xlsx; " "parse_stock пропущен (%d поз.).",
                len(all_items),
            )
        return len(all_items), len(filtered)

    fp = current_xlsx_fingerprint()
    all_items = parse_stock()
    save_items(all_items, source_fingerprint=fp)
    filtered = filter_by_threshold(all_items)
    generate_filtered_xlsx(filtered)

    send_group_limit_alerts_for_items(all_items, log_context="after_parse")

    from services.zero_sold import run_zero_sold_check_after_parse

    run_zero_sold_check_after_parse(all_items)

    return len(all_items), len(filtered)
