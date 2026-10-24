"""
Red Bin: точечный лимит по одной позиции и три стадии предупреждения.

Контракт — ``docs/red_bin.md``. Проверка вызывается из полного парса
(``services.stock_pipeline.run_parse_and_save_filtered``), не по cron.
"""

from __future__ import annotations

import logging
from html import escape as html_escape
from typing import Any, Optional

from bot.telegram_bridge import send_group_limit_alert_html
from parser.excel_parser import normalize_top_category_label
from services.zero_sold import make_position_key
from storage.data_store import list_red_bin, save_red_bin_items

logger = logging.getLogger(__name__)

# rank 1..3. Срез ``[stored:new_rank]`` даёт стадии, которые только что пересекли.
_STAGES: tuple[str, ...] = ("at", "eating", "gone")

_STAGE_LINE: dict[str, str] = {
    "at": "дошли до лимита, в корзине {limit} шт.",
    "eating": "начали продавать из корзины",
    "gone": "продали всё, остаток 0",
}

# Пустой бренд в кнопках каталога. В ключ K1 и в запись реестра не подставляется.
NO_BRAND_LABEL: str = "Без бренда"


def zone_rank(qty: Optional[int], limit: int) -> int:
    """
    Зона остатка относительно лимита Red Bin.

    ``None`` и ``qty <= 0`` — пропавшая строка или ноль, rank ``gone``.

    :param qty: Текущий остаток или ``None``, если строки с этим ключом нет.
    :type qty: Optional[int]
    :param limit: Лимит ``L`` (целое ``>= 1``).
    :type limit: int
    :returns: 0 выше лимита, 1 ровно ``L``, 2 между 1 и ``L-1``, 3 ноль или нет строки.
    :rtype: int

    :example:
    >>> zone_rank(15, 5)
    0
    >>> zone_rank(5, 5)
    1
    >>> zone_rank(None, 5)
    3
    """
    if qty is None or qty <= 0:
        return 3
    if qty < limit:
        return 2
    if qty == limit:
        return 1
    return 0


def advance_rank(stored: int, new_rank: int) -> tuple[int, tuple[str, ...]]:
    """
    Считает новый сохранённый rank и стадии, которые надо прислать один раз.

    Выше лимита (``new_rank == 0``) сбрасывает цикл без сообщения.
    Рост rank отдаёт все промежуточные стадии одним кортежем.
    Падение rank внутрь корзины (частичный возврат) сообщений не даёт
    и заново взводит верхние стадии.

    :param stored: Rank с прошлого снимка, 0..3.
    :type stored: int
    :param new_rank: Rank текущего снимка, 0..3.
    :type new_rank: int
    :returns: ``(новый rank, стадии по порядку)``. Пустой кортеж — не слать.
    :rtype: tuple[int, tuple[str, ...]]

    :example:
    >>> advance_rank(0, 3)
    (3, ('at', 'eating', 'gone'))
    >>> advance_rank(2, 2)
    (2, ())
    """
    stored = max(0, min(3, int(stored)))
    new_rank = max(0, min(3, int(new_rank)))
    if new_rank == 0:
        return 0, ()
    if new_rank > stored:
        return new_rank, _STAGES[stored:new_rank]
    if new_rank < stored:
        return new_rank, ()
    return stored, ()


def format_red_bin_alert_html(
    rec: dict[str, Any],
    stages: tuple[str, ...],
    *,
    prev_qty: int,
    now_qty: Optional[int],
) -> Optional[str]:
    """
    Одно HTML-сообщение по позициям, чьи стадии пересечены этим снимком.

    :param rec: Запись реестра (``name``, ``brand``, ``group``, ``limit``).
    :type rec: dict[str, Any]
    :param stages: Стадии из :func:`advance_rank`. Пустой кортеж — не о чём слать.
    :type stages: tuple[str, ...]
    :param prev_qty: Остаток до этого снимка (``last_qty``).
    :type prev_qty: int
    :param now_qty: Остаток сейчас. ``None`` — строки в файле нет.
    :type now_qty: Optional[int]
    :returns: HTML или ``None``, если стадий нет.
    :rtype: Optional[str]
    """
    if not stages:
        return None
    limit = int(rec["limit"])
    now_label = "нет в файле" if now_qty is None else str(now_qty)
    lines = [
        "🔴 <b>Red Bin</b>",
        html_escape(str(rec.get("name") or "")),
        f"Бренд: {html_escape(str(rec.get('brand') or ''))}",
        f"Группа: {html_escape(str(rec.get('group') or ''))}",
        f"Лимит: <code>{limit}</code>",
        f"Было → стало: <code>{prev_qty}</code> → <code>{html_escape(now_label)}</code>",
    ]
    for stage in stages:
        template = _STAGE_LINE.get(stage, stage)
        lines.append("• " + template.format(limit=limit))
    return "\n".join(lines)


def index_snapshot(items: list[dict]) -> dict[str, dict]:
    """
    Карта K1 → позиция снимка. Дубликаты ключа схлопываются в max ``quantity``.

    :param items: Строки ``parse_stock``.
    :type items: list[dict]
    :returns: Словарь ``key -> {key, group, brand, name, quantity}``.
    :rtype: dict[str, dict]
    """
    out: dict[str, dict] = {}
    for raw in items:
        built = _compact_row(raw)
        if built is None:
            continue
        prev = out.get(built["key"])
        if prev is None or int(built["quantity"]) > int(prev["quantity"]):
            out[built["key"]] = built
    return out


def browse_tree(items: list[dict]) -> dict[str, dict[str, dict[str, list[dict]]]]:
    """
    Каталог кнопок: группа → раздел (indent 8) → бренд (indent 10) → модели.

    В выгрузке мячей indent 10 — это бренд (``Jogel``), indent 12 — модель
    (``Мяч волейбольный Jogel JV-100``). Если у бренда нет моделей, листом
    остаётся сама строка indent 10. Строки indent 12 под одним ``line`` не
    дублируют заголовок бренда. Дубликаты K1 внутри полки — max ``quantity``.

    :param items: Строки ``parse_stock`` (поля ``indent`` и ``line``).
    :type items: list[dict]
    :returns: ``группа → раздел → бренд → список моделей``.
    :rtype: dict[str, dict[str, dict[str, list[dict]]]]

    :example:
    >>> tree = browse_tree([
    ...     {"top_category": "Мячи", "brand": "Волейбол", "name": "Jogel",
    ...      "quantity": 10, "indent": 10, "line": "Jogel"},
    ...     {"top_category": "Мячи", "brand": "Волейбол", "name": "JV-100",
    ...      "quantity": 5, "indent": 12, "line": "Jogel"},
    ... ])
    >>> tree["Мячи"]["Волейбол"]["Jogel"][0]["name"]
    'JV-100'
    """
    shelves: dict[str, dict[str, dict[str, dict[str, dict]]]] = {}
    for raw in items:
        built = _compact_row(raw)
        if built is None:
            continue
        try:
            indent = int(raw.get("indent") or 10)
        except (TypeError, ValueError):
            indent = 10
        built["indent"] = indent
        line = str(raw.get("line") or built["name"]).strip() or built["name"]
        section = str(built["brand"] or "") or NO_BRAND_LABEL
        shelf = shelves.setdefault(str(built["group"]), {}).setdefault(section, {}).setdefault(line, {})
        prev = shelf.get(built["key"])
        # Заголовок бренда (indent 10) не затирает модель с тем же ключом.
        if prev is not None and int(prev.get("indent") or 10) > indent:
            continue
        if prev is None or indent > int(prev.get("indent") or 10) or int(built["quantity"]) > int(prev["quantity"]):
            shelf[built["key"]] = built

    tree: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for group in sorted(shelves, key=str.casefold):
        tree[group] = {}
        for section in sorted(shelves[group], key=str.casefold):
            lines: dict[str, list[dict]] = {}
            for line in sorted(shelves[group][section], key=str.casefold):
                rows = list(shelves[group][section][line].values())
                models = [row for row in rows if int(row.get("indent") or 10) >= 12]
                if models:
                    chosen = models
                else:
                    chosen = rows
                lines[line] = sorted(chosen, key=lambda row: str(row["name"]).casefold())
            tree[group][section] = lines
    return tree


def apply_red_bin(items: list[dict]) -> None:
    """
    Сравнивает реестр Red Bin со снимком и шлёт новые стадии в чат лимитов.

    Пустой реестр — тихий выход. Ошибки ловятся здесь: парсинг остатков
    из-за алерта не откатывается. Rank пишется до отправки, чтобы повтор
    того же снимка не дублировал сообщение.

    :param items: Полный список позиций после ``parse_stock``.
    :type items: list[dict]
    :returns: None
    :side effects: ``storage/red_bin.json`` и Telegram, если есть новые стадии.
    """
    try:
        _apply_red_bin(items)
    except Exception:
        logger.error("red_bin phase=apply result=unexpected_error", exc_info=True)


def _compact_row(raw: dict) -> Optional[dict]:
    """Одна строка снимка в поля реестра или ``None``, если qty не число."""
    qty_raw = raw.get("quantity")
    if qty_raw is None:
        return None
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    group = normalize_top_category_label(str(raw.get("top_category") or raw.get("group") or ""))
    brand = str(raw.get("brand") or "").strip()
    return {
        "key": make_position_key(raw),
        "group": group,
        "brand": brand,
        "name": name,
        "quantity": qty,
    }


def _apply_red_bin(items: list[dict]) -> None:
    registry = list_red_bin()
    if not registry:
        logger.info("red_bin phase=skip reason=empty")
        return

    stock = index_snapshot(items)
    pending: list[str] = []
    stage_log: list[str] = []
    changed = False

    for item_id, rec in registry.items():
        try:
            limit = int(rec["limit"])
            stored = int(rec.get("rank") or 0)
            prev_qty = int(rec.get("last_qty") or 0)
        except (TypeError, ValueError, KeyError):
            logger.warning("red_bin skip bad record id=%s", item_id)
            continue
        if limit < 1:
            continue

        row = stock.get(str(rec.get("key") or ""))
        now_qty: Optional[int] = int(row["quantity"]) if row is not None else None
        new_rank, stages = advance_rank(stored, zone_rank(now_qty, limit))
        stored_qty = 0 if now_qty is None else now_qty
        if new_rank != stored or stored_qty != prev_qty:
            rec["rank"] = new_rank
            rec["last_qty"] = stored_qty
            changed = True
        if not stages:
            continue
        html = format_red_bin_alert_html(rec, stages, prev_qty=prev_qty, now_qty=now_qty)
        if not html:
            continue
        pending.append(html)
        stage_log.append(f"{item_id}:{'+'.join(stages)}")

    if changed:
        save_red_bin_items(registry)

    for html in pending:
        send_group_limit_alert_html(html)

    logger.info(
        "red_bin registry=%d messages=%d stages=%s",
        len(registry),
        len(pending),
        ",".join(stage_log) if stage_log else "-",
    )


def red_bin_report_rows(registry: dict[str, dict], stock: dict[str, dict]) -> list[dict]:
    """
    Все позиции реестра с лимитом и текущим остатком из снимка.

    :param registry: ``id -> запись`` из :func:`storage.data_store.list_red_bin`.
    :type registry: dict[str, dict]
    :param stock: Карта K1 из :func:`index_snapshot`. Ключа нет — остаток ``None``.
    :type stock: dict[str, dict]
    :returns: Строки по имени: group, brand, name, limit, qty.
    :rtype: list[dict]
    """
    rows: list[dict] = []
    for rec in registry.values():
        try:
            limit = int(rec["limit"])
        except (TypeError, ValueError, KeyError):
            continue
        if limit < 1:
            continue
        hit = stock.get(str(rec.get("key") or ""))
        qty: Optional[int]
        if hit is None:
            qty = None
        else:
            try:
                qty = int(hit["quantity"])
            except (TypeError, ValueError, KeyError):
                qty = None
        rows.append(
            {
                "group": str(rec.get("group") or ""),
                "brand": str(rec.get("brand") or ""),
                "name": str(rec.get("name") or ""),
                "limit": limit,
                "qty": qty,
            }
        )
    rows.sort(key=lambda row: (row["group"].casefold(), row["name"].casefold()))
    return rows


def format_red_bin_report_html(rows: list[dict]) -> list[str]:
    """
    HTML-сообщения со всеми лимитами. Длинный список режется пачками до 3500 символов.

    :param rows: Результат :func:`red_bin_report_rows`.
    :type rows: list[dict]
    :returns: Одно или несколько сообщений. Пустой список строк — пустой результат.
    :rtype: list[str]
    """
    if not rows:
        return []
    lines = [f"📋 <b>Red Bin</b> — {len(rows)} поз."]
    for row in rows:
        qty = "нет в снимке" if row["qty"] is None else str(row["qty"])
        place = html_escape(row["group"])
        if row["brand"]:
            place = f"{place} / {html_escape(row['brand'])}"
        lines.append(
            f"• {html_escape(row['name'])} ({place}) — "
            f"лимит <code>{row['limit']}</code>, сейчас <code>{html_escape(qty)}</code>"
        )
    messages: list[str] = []
    chunk: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + 1
        if chunk and size + extra > 3500:
            messages.append("\n".join(chunk))
            chunk = [line]
            size = extra
        else:
            chunk.append(line)
            size += extra
    if chunk:
        messages.append("\n".join(chunk))
    return messages


def write_red_bin_xlsx(rows: list[dict], path: str) -> None:
    """
    Пишет xlsx со всеми лимитами Red Bin.

    :param rows: Результат :func:`red_bin_report_rows`.
    :type rows: list[dict]
    :param path: Куда сохранить файл.
    :type path: str
    :returns: None
    :side effects: Создаёт xlsx по ``path``.
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Red Bin"
    ws.append(["Группа", "Бренд", "Название", "Лимит", "Сейчас"])
    for row in rows:
        ws.append(
            [
                row["group"],
                row["brand"],
                row["name"],
                row["limit"],
                "" if row["qty"] is None else row["qty"],
            ]
        )
    wb.save(path)
