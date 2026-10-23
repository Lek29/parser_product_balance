"""
Недельные снимки остатков для zero-sold push (docs/push_message_about_zero.md).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Optional

import config
from services.calendar_week import now_moscow
from storage.data_store import atomic_write_json

logger = logging.getLogger(__name__)


def snapshot_path(week_id: str) -> str:
    """
    Путь к JSON-снимку недели.

    :param week_id: Пн недели в формате ``YYYY-MM-DD``.
    :type week_id: str
    :returns: Абсолютный/проектный путь файла.
    :rtype: str
    """
    safe = week_id.strip().replace("..", "")
    return os.path.join(config.WEEKLY_SNAPSHOTS_DIR, f"{safe}.json")


def build_snapshot_from_items(
    items: list[dict],
    *,
    week_id: str,
    source_xlsx_mtime: Optional[float] = None,
) -> dict[str, Any]:
    """
    Собирает сериализуемый снимок позиций для хранения.

    Дубликаты по ``key``: оставляется позиция с **максимальным** ``quantity``.

    :param items: Результат :func:`parser.excel_parser.parse_stock`.
    :type items: list[dict]
    :param week_id: Идентификатор недели (Пн ISO).
    :type week_id: str
    :param source_xlsx_mtime: Опциональный mtime ``current.xlsx``.
    :type source_xlsx_mtime: Optional[float]
    :returns: Словарь снимка.
    :rtype: dict[str, Any]
    """
    from services.zero_sold import make_position_key
    from parser.excel_parser import normalize_top_category_label

    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        try:
            qty = int(item.get("quantity"))
        except (TypeError, ValueError):
            continue
        key = make_position_key(item)
        group = normalize_top_category_label(str(item.get("top_category", "") or ""))
        brand = str(item.get("brand") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        prev = by_key.get(key)
        if prev is None or qty > int(prev["quantity"]):
            by_key[key] = {
                "key": key,
                "group": group,
                "brand": brand,
                "name": name,
                "quantity": qty,
            }

    payload: dict[str, Any] = {
        "week_id": week_id,
        "saved_at": now_moscow().isoformat(),
        "positions": list(by_key.values()),
    }
    if source_xlsx_mtime is not None:
        payload["source_xlsx_mtime"] = float(source_xlsx_mtime)
    return payload


def save_week_snapshot(week_id: str, payload: dict[str, Any]) -> str:
    """
    Атомарно пишет снимок недели на диск.

    :param week_id: Пн недели ``YYYY-MM-DD``.
    :type week_id: str
    :param payload: Объект снимка.
    :type payload: dict[str, Any]
    :returns: Путь записанного файла.
    :rtype: str
    :side effects: Создаёт каталог снимков и файл JSON.
    """
    os.makedirs(config.WEEKLY_SNAPSHOTS_DIR, exist_ok=True)
    path = snapshot_path(week_id)
    atomic_write_json(path, payload, indent=2, ensure_ascii=False)
    logger.info(
        "Недельный снимок сохранён: week_id=%s positions=%d path=%s",
        week_id,
        len(payload.get("positions") or []),
        path,
    )
    return path


def load_week_snapshot(week_id: str) -> Optional[dict[str, Any]]:
    """
    Читает снимок недели с диска.

    :param week_id: Пн недели ``YYYY-MM-DD``.
    :type week_id: str
    :returns: Словарь или ``None``, если файла нет / JSON битый.
    :rtype: Optional[dict[str, Any]]
    """
    import json

    path = snapshot_path(week_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Не удалось прочитать снимок %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    return data
