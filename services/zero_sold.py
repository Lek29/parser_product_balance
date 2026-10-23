"""
Детектор позиций, ушедших в ноль между календарными неделями (K1, A+B).

См. ``docs/push_message_about_zero.md``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

import config
from parser.excel_parser import normalize_top_category_label
from services.calendar_week import (
    previous_week_monday,
    today_moscow,
    week_monday,
)
from storage.data_store import atomic_write_json, current_xlsx_fingerprint

logger = logging.getLogger(__name__)

Reason = Literal["zero", "missing"]


@dataclass(frozen=True)
class ZeroSoldHit:
    """
    Одна позиция, ушедшая в ноль относительно прошлой недели.

    :ivar key: Ключ K1.
    :ivar group: Товарная группа.
    :ivar brand: Бренд.
    :ivar name: Наименование.
    :ivar qty_was: Количество на прошлой неделе (> 0).
    :ivar reason: ``zero`` — явный 0; ``missing`` — строки нет.
    """

    key: str
    group: str
    brand: str
    name: str
    qty_was: int
    reason: Reason


def make_position_key(item: dict) -> str:
    """
    Ключ позиции K1: группа + бренд + название (casefold/strip).

    :param item: Словарь позиции из ``parse_stock``.
    :type item: dict
    :returns: Стабильный строковый ключ.
    :rtype: str
    """
    group = normalize_top_category_label(str(item.get("top_category", "") or ""))
    brand = str(item.get("brand") or "").strip().casefold()
    name = str(item.get("name") or "").strip().casefold()
    return f"{group.casefold()}|{brand}|{name}"


def _positions_to_map(positions: list[dict]) -> dict[str, dict]:
    """
    Строит map key → position; при дубликатах берёт max quantity.

    :param positions: Список словарей с полем ``key`` или сырые items.
    :type positions: list[dict]
    :returns: Словарь по ключу.
    :rtype: dict[str, dict]
    """
    out: dict[str, dict] = {}
    for raw in positions:
        if "key" in raw and "quantity" in raw:
            key = str(raw["key"])
            try:
                qty = int(raw["quantity"])
            except (TypeError, ValueError):
                continue
            entry = {
                "key": key,
                "group": str(raw.get("group") or ""),
                "brand": str(raw.get("brand") or ""),
                "name": str(raw.get("name") or ""),
                "quantity": qty,
            }
        else:
            key = make_position_key(raw)
            try:
                qty = int(raw.get("quantity"))
            except (TypeError, ValueError):
                continue
            entry = {
                "key": key,
                "group": normalize_top_category_label(
                    str(raw.get("top_category", "") or "")
                ),
                "brand": str(raw.get("brand") or "").strip(),
                "name": str(raw.get("name") or "").strip(),
                "quantity": qty,
            }
        prev = out.get(key)
        if prev is None or qty > int(prev["quantity"]):
            out[key] = entry
    return out


def find_zero_sold(
    prev_positions: list[dict],
    curr_items: list[dict],
) -> list[ZeroSoldHit]:
    """
    Находит позиции с прошлой недели (qty > 0), ставшие 0 или пропавшие.

    :param prev_positions: Позиции снимка прошлой недели.
    :type prev_positions: list[dict]
    :param curr_items: Текущий результат ``parse_stock``.
    :type curr_items: list[dict]
    :returns: Список hit'ов, отсортированный по группе/имени.
    :rtype: list[ZeroSoldHit]
    """
    prev_map = _positions_to_map(prev_positions)
    curr_map = _positions_to_map(curr_items)
    hits: list[ZeroSoldHit] = []
    for key, prev in prev_map.items():
        qty_was = int(prev["quantity"])
        if qty_was <= 0:
            continue
        if key not in curr_map:
            hits.append(
                ZeroSoldHit(
                    key=key,
                    group=prev["group"],
                    brand=prev["brand"],
                    name=prev["name"],
                    qty_was=qty_was,
                    reason="missing",
                )
            )
            continue
        curr_qty = int(curr_map[key]["quantity"])
        if curr_qty == 0:
            hits.append(
                ZeroSoldHit(
                    key=key,
                    group=prev["group"],
                    brand=prev["brand"],
                    name=prev["name"],
                    qty_was=qty_was,
                    reason="zero",
                )
            )
    hits.sort(key=lambda h: (h.group.casefold(), h.name.casefold(), h.brand.casefold()))
    return hits


def load_zero_sold_state() -> dict[str, Any]:
    """
    Читает ``storage/zero_sold_state.json``.

    :returns: Словарь состояния или пустой dict.
    :rtype: dict[str, Any]
    """
    import json

    path = config.ZERO_SOLD_STATE_JSON_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("zero_sold_state повреждён: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def save_zero_sold_state(
    *,
    week_from: str,
    week_to: str,
    n_hits: int,
    filename: Optional[str] = None,
) -> None:
    """
    Пишет идемпотентное состояние последнего сравнения недель.

    :param week_from: Пн прошлой недели.
    :type week_from: str
    :param week_to: Пн текущей недели.
    :type week_to: str
    :param n_hits: Число позиций в отчёте.
    :type n_hits: int
    :param filename: Имя отправленного файла или ``None``.
    :type filename: Optional[str]
    :returns: None
    :side effects: Обновляет ``zero_sold_state.json``.
    """
    payload: dict[str, Any] = {
        "last_compared_from": week_from,
        "last_compared_to": week_to,
        "n_hits": n_hits,
        "pushed_at": datetime.now().isoformat(),
    }
    if filename:
        payload["filename"] = filename
    atomic_write_json(
        config.ZERO_SOLD_STATE_JSON_PATH,
        payload,
        indent=2,
        ensure_ascii=False,
    )


def already_pushed_for_weeks(week_from: str, week_to: str) -> bool:
    """
    Проверяет, был ли уже push/сравнение для пары недель.

    :param week_from: Пн прошлой недели.
    :type week_from: str
    :param week_to: Пн текущей недели.
    :type week_to: str
    :returns: ``True``, если пара уже обработана.
    :rtype: bool
    """
    state = load_zero_sold_state()
    return (
        state.get("last_compared_from") == week_from
        and state.get("last_compared_to") == week_to
    )


def run_zero_sold_check_after_parse(all_items: list[dict]) -> None:
    """
    Хук после полного парса: снимок недели, diff с прошлой, xlsx в личку.

    Ошибки глотаются (лог), чтобы не ронять пайплайн остатков.

    :param all_items: Актуальный список позиций после ``parse_stock``.
    :type all_items: list[dict]
    :returns: None
    :side effects: JSON-снимки, state, temp xlsx, Telegram ``send_document``.
    """
    try:
        _run_zero_sold_check_after_parse_impl(all_items)
    except Exception:
        logger.error(
            "zero_sold phase=fail result=unexpected_error",
            exc_info=True,
        )


def _run_zero_sold_check_after_parse_impl(all_items: list[dict]) -> None:
    """
    Внутренняя реализация :func:`run_zero_sold_check_after_parse` без try/except.

    :param all_items: Позиции текущего parse.
    :type all_items: list[dict]
    :returns: None
    """
    from bot.telegram_bridge import send_zero_sold_xlsx
    from services.zero_sold_report import generate_zero_sold_xlsx
    from storage.weekly_stock_snapshot import (
        build_snapshot_from_items,
        load_week_snapshot,
        save_week_snapshot,
    )

    today = today_moscow()
    week_to_d = week_monday(today)
    week_from_d = previous_week_monday(today)
    week_to = week_to_d.isoformat()
    week_from = week_from_d.isoformat()

    fp = current_xlsx_fingerprint()
    mtime = fp[0] if fp else None
    payload = build_snapshot_from_items(
        all_items, week_id=week_to, source_xlsx_mtime=mtime
    )
    save_week_snapshot(week_to, payload)

    if already_pushed_for_weeks(week_from, week_to):
        logger.info(
            "zero_sold phase=skip reason=already_compared "
            "week_from=%s week_to=%s",
            week_from,
            week_to,
        )
        return

    prev = load_week_snapshot(week_from)
    if prev is None:
        logger.info(
            "zero_sold phase=skip reason=no_prev_snapshot week_from=%s week_to=%s",
            week_from,
            week_to,
        )
        # Не фиксируем state: когда появится prev, сравним; но без prev
        # повторные прогоны этой недели снова skip по no_prev — ок.
        # Чтобы не долбить лог каждую актуализацию без prev — всё же
        # помечаем «сравнили с пустым» только если хотим тишины.
        # План: нет prev → no-op без state. Оставляем без state.
        return

    prev_positions = prev.get("positions") or []
    if not isinstance(prev_positions, list):
        prev_positions = []

    hits = find_zero_sold(prev_positions, all_items)
    logger.info(
        "zero_sold phase=compare week_from=%s week_to=%s n_hits=%d",
        week_from,
        week_to,
        len(hits),
    )

    if not hits:
        save_zero_sold_state(week_from=week_from, week_to=week_to, n_hits=0)
        logger.info("zero_sold phase=send result=skipped reason=no_hits")
        return

    report_date = today.isoformat()
    filename = f"ушли_в_ноль_{report_date}.xlsx"
    path = os.path.join(config.TEMP_DIR, filename)
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    generate_zero_sold_xlsx(hits, path)

    caption = (
        f"Ушли в 0 относительно прошлой недели "
        f"({week_from} → {week_to}): {len(hits)} поз."
    )
    sent = send_zero_sold_xlsx(path, caption=caption, filename=filename)
    if sent:
        save_zero_sold_state(
            week_from=week_from,
            week_to=week_to,
            n_hits=len(hits),
            filename=filename,
        )
        logger.info(
            "zero_sold phase=send result=ok n_hits=%d file=%s",
            len(hits),
            filename,
        )
    else:
        logger.warning(
            "zero_sold phase=send result=fail n_hits=%d — state не обновлён",
            len(hits),
        )

    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        logger.debug("Не удалось удалить temp zero-sold xlsx", exc_info=True)
