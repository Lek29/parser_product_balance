"""
Модуль парсинга Excel-таблицы с остатками товаров.

Читает storage/current.xlsx, определяет иерархию товаров по отступу ячеек (indent),
фильтрует позиции с количеством <= STOCK_THRESHOLD и формирует структурированный список.
Список групп — :func:`list_product_groups`; частичный отбор —
:func:`filter_by_group_thresholds`; отдельный файл частичной выгрузки —
:func:`generate_filtered_partial_xlsx`. Глобальный отфильтрованный .xlsx —
:func:`generate_filtered_xlsx`. Пороги по группам (роль fourty_role) —
:func:`find_group_limit_violations`.

Структура иерархии по indent:
    indent 4  — категория верхнего уровня (Аксессуары, Мячи)
    indent 6  — подкатегория (Гетры, Очки для плавания)
    indent 8  — бренд (25Degrees, Atemi, Speedo)
    indent 10 — товарная группа / SKU (товарная позиция)
    indent 12 — конкретный вариант (цвет, размер)

Реальные товарные позиции: только indent 10 и 12.
"""

import logging
import math
import os
from itertools import groupby
from typing import Any, Optional

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import config

logger = logging.getLogger(__name__)

# Строки заголовка в файле — пропускаем (1-индексация)
HEADER_ROWS = 7

# Отступы, соответствующие реальным товарным позициям
PRODUCT_INDENTS = {10.0, 12.0}

# Соответствие indent → ключ контекста
INDENT_TO_LEVEL = {
    4.0: "top_category",
    6.0: "subcategory",
    8.0: "brand",
}

# Имя группы для позиций без категории верхнего уровня (пустой top_category после parse_stock).
TOP_CATEGORY_FALLBACK_LABEL: str = "Без группы"

# Читаем фиксированный диапазон A..E: кол-во в D, цена в E. Без max_col у «узких» строк
# iter_rows отдаёт кортеж короче индексов 3/4 → IndexError на части выгрузок с почты.
_STOCK_SHEET_MIN_COL: int = 1
_STOCK_SHEET_MAX_COL: int = 5


def parse_stock(xlsx_path: Optional[str] = None) -> list[dict]:
    """
    Читает xlsx-файл остатков и возвращает список всех товарных позиций.

    Определяет иерархию по отступу ячеек (indent). Возвращает позиции
    с indent 10 и 12, обогащённые контекстом родительских уровней.
    Строки заголовка (1-7) пропускаются.

    :param xlsx_path: Путь к xlsx-файлу. По умолчанию config.CURRENT_XLSX_PATH.
    :type xlsx_path: Optional[str]
    :returns: Список словарей с полями:
              top_category, subcategory, brand, name, quantity, price.
    :rtype: list[dict]
    :raises FileNotFoundError: Если файл не найден.
    :raises openpyxl.utils.exceptions.InvalidFileException: Если файл повреждён.

    Для каждой строки данных запрашиваются колонки A–E (см. константы диапазона),
    чтобы пустые хвосты строк не сокращали кортеж ``row`` ниже индексов колонок D/E.

    :example:
    >>> items = parse_stock()
    >>> print(items[0])
    {'top_category': 'Аксессуары', 'subcategory': 'Гетры', 'brand': 'Accapi',
     'name': 'Гетры Accapi 950 Ski Thermic, размер 45-47', 'quantity': 2, 'price': 1759}
    """
    path = xlsx_path or config.CURRENT_XLSX_PATH

    if not os.path.exists(path):
        raise FileNotFoundError(f"Файл остатков не найден: {path}")

    wb = openpyxl.load_workbook(path)
    ws = wb.active

    # Текущий контекст иерархии: {indent: название}
    context: dict[float, str] = {}
    items = []

    for row_idx, row in enumerate(
        ws.iter_rows(
            min_row=HEADER_ROWS + 1,
            min_col=_STOCK_SHEET_MIN_COL,
            max_col=_STOCK_SHEET_MAX_COL,
        ),
        start=HEADER_ROWS + 1,
    ):
        cell_name = row[0]  # колонка A
        cell_qty = row[3]   # колонка D
        cell_price = row[4]  # колонка E

        name = cell_name.value
        if name is None:
            continue

        name = str(name).strip()
        if not name:
            continue

        indent = cell_name.alignment.indent if cell_name.alignment else 0.0
        indent = float(indent)

        # Обновляем контекст для категорий (indent 4, 6, 8)
        if indent in INDENT_TO_LEVEL:
            context[indent] = name
            # Сбрасываем все дочерние уровни при смене родителя
            for child_indent in list(context.keys()):
                if child_indent > indent:
                    del context[child_indent]
            continue

        # Обрабатываем реальные товарные позиции (indent 10, 12)
        if indent in PRODUCT_INDENTS:
            qty = cell_qty.value
            price = cell_price.value

            # Пропускаем строки без количества
            if qty is None:
                continue

            try:
                qty = int(qty)
                price = int(price) if price is not None else None
            except (ValueError, TypeError):
                logger.warning("Строка %d: не удалось привести qty/price к int: %s / %s",
                               row_idx, qty, price)
                continue

            items.append({
                "top_category": context.get(4.0, ""),
                "subcategory": context.get(6.0, ""),
                "brand": context.get(8.0, ""),
                "name": name,
                "quantity": qty,
                "price": price,
            })

    logger.info("Всего товарных позиций в файле: %d", len(items))
    return items


def normalize_top_category_label(raw: str) -> str:
    """
    Приводит сырое значение ``top_category`` к имени группы для UI и словарей порогов.

    Пустая строка и строка из пробелов заменяются на :data:`TOP_CATEGORY_FALLBACK_LABEL`,
    чтобы в списках групп не было «пустых» кнопок и ключи совпадали с :func:`list_product_groups`.

    :param raw: Значение поля ``top_category`` из словаря позиции.
    :type raw: str
    :returns: Непустая метка группы.
    :rtype: str
    """
    text = (raw or "").strip()
    return text if text else TOP_CATEGORY_FALLBACK_LABEL


def list_product_groups(xlsx_path: Optional[str] = None) -> list[str]:
    """
    Возвращает уникальные группы товара верхнего уровня для reply-меню частичной фильтрации.

    Источник данных — тот же, что у :func:`parse_stock`: по умолчанию актуальный
    ``storage/current.xlsx`` (после «Актуализировать» или фоновой загрузки с почты).
    Учитываются только товарные строки с числовым количеством; заголовки категорий
    без ни одной такой позиции в выборку не попадают.

    :param xlsx_path: Путь к xlsx. По умолчанию :data:`config.CURRENT_XLSX_PATH`.
    :type xlsx_path: Optional[str]
    :returns: Отсортированный по алфавиту список имён групп (пустой ``top_category``
              даёт одну группу :data:`TOP_CATEGORY_FALLBACK_LABEL`).
    :rtype: list[str]

    :example:
    >>> groups = list_product_groups()
    >>> # ['Аксессуары', 'Велосипеды', ...]
    """
    items = parse_stock(xlsx_path)
    labels = {normalize_top_category_label(item.get("top_category", "")) for item in items}
    return sorted(labels, key=lambda s: s.casefold())


def filter_by_threshold(
    items: list[dict],
    threshold: Optional[int] = None,
) -> list[dict]:
    """
    Фильтрует список товаров по пороговому значению количества.

    :param items: Список товарных позиций (результат parse_stock).
    :type items: list[dict]
    :param threshold: Пороговое количество включительно. По умолчанию config.STOCK_THRESHOLD.
    :type threshold: Optional[int]
    :returns: Список товаров, у которых quantity <= threshold.
    :rtype: list[dict]

    :example:
    >>> all_items = parse_stock()
    >>> low_stock = filter_by_threshold(all_items, threshold=3)
    >>> print(f"Мало на складе: {len(low_stock)} позиций")
    """
    limit = threshold if threshold is not None else config.STOCK_THRESHOLD
    result = [item for item in items if item["quantity"] <= limit]
    logger.info("Позиций с остатком <= %d: %d", limit, len(result))
    return result


def filter_by_group_thresholds(
    items: list[dict],
    thresholds_by_group: dict[str, int],
) -> list[dict]:
    """
    Частичная фильтрация: для каждой группы свой порог ``quantity <= порог``.

    Имя группы берётся как :func:`normalize_top_category_label` от поля
    ``top_category`` позиции — те же строки, что в :func:`list_product_groups`
    и в черновике порогов бота.

    **Поведение для групп без ключа в словаре:** позиции этих групп **не
    включаются** в результат. В выгрузку попадают только группы, для которых
    пользователь задал порог, и только строки с количеством не выше этого
    порога.

    При пустом ``thresholds_by_group`` возвращается пустой список.

    :param items: Полный список позиций (результат :func:`parse_stock`).
    :type items: list[dict]
    :param thresholds_by_group: Нормализованное имя группы → порог включительно.
    :type thresholds_by_group: dict[str, int]
    :returns: Позиции, прошедшие условие по своей группе.
    :rtype: list[dict]
    :side effects: Нет (чистая функция); пишет в лог.

    :example:
    >>> all_items = parse_stock()
    >>> rules = {"Велосипеды": 3, "Бассейн": 5}
    >>> low = filter_by_group_thresholds(all_items, rules)
    """
    if not thresholds_by_group:
        logger.info("Частичный фильтр: словарь порогов пуст — 0 позиций.")
        return []

    result: list[dict] = []
    for item in items:
        group = normalize_top_category_label(item.get("top_category", ""))
        if group not in thresholds_by_group:
            continue
        limit = thresholds_by_group[group]
        if item["quantity"] <= limit:
            result.append(item)

    logger.info(
        "Частичный фильтр: правил %d, позиций в результате: %d",
        len(thresholds_by_group),
        len(result),
    )
    return result


def find_group_limit_violations(
    items: list[dict],
    limits_by_group: dict[str, int],
) -> list[dict[str, Any]]:
    """
    Находит позиции с остатком не выше лимита для каждой группы из словаря лимитов.

    Используется для уведомлений о порогах по товарной группе (см. ``docs/fourty_role.md``):
    для каждой группы с заданным лимитом перечисляются только те SKU (строки
    с indent 10/12 из :func:`parse_stock`), у которых ``quantity <= лимит``.

    Ключи групп нормализуются через :func:`normalize_top_category_label`, как
    в :func:`filter_by_group_thresholds`.

    :param items: Полный список позиций (результат :func:`parse_stock`).
    :type items: list[dict]
    :param limits_by_group: Имя группы → лимит включительно (как в хранилище лимитов).
    :type limits_by_group: dict[str, int]
    :returns: Список словарей вида ``{"group", "limit", "positions"}``, только
              для групп, где есть хотя бы одна позиция с ``quantity <= лимит``.
              В ``positions`` — ``{"name", "quantity"}``, отсортировано по имени.
    :rtype: list[dict[str, Any]]
    :side effects: Пишет в лог.

    :example:
    >>> items = [
    ...     {"top_category": "Велосипеды", "name": "A", "quantity": 2},
    ...     {"top_category": "Велосипеды", "name": "B", "quantity": 10},
    ... ]
    >>> find_group_limit_violations(items, {"Велосипеды": 5})
    [{'group': 'Велосипеды', 'limit': 5, 'positions': [{'name': 'A', 'quantity': 2}]}]
    """
    if not limits_by_group:
        logger.info("Пороги по группам: словарь лимитов пуст — срабатываний нет.")
        return []

    normalized_limits: dict[str, int] = {}
    for raw_key, raw_limit in limits_by_group.items():
        group_key = normalize_top_category_label(str(raw_key))
        try:
            limit_val = int(raw_limit)
        except (TypeError, ValueError):
            continue
        if limit_val < 0:
            continue
        normalized_limits[group_key] = limit_val

    if not normalized_limits:
        logger.info("Пороги по группам: после нормализации лимитов не осталось.")
        return []

    out: list[dict[str, Any]] = []
    for group in sorted(normalized_limits.keys(), key=lambda s: s.casefold()):
        limit = normalized_limits[group]
        positions: list[dict[str, Any]] = []
        for item in items:
            item_group = normalize_top_category_label(item.get("top_category", ""))
            if item_group != group:
                continue
            qty = item.get("quantity")
            if qty is None:
                continue
            try:
                qty_int = int(qty)
            except (TypeError, ValueError):
                continue
            if qty_int <= limit:
                positions.append({"name": item.get("name", ""), "quantity": qty_int})
        positions.sort(key=lambda p: str(p.get("name", "")).casefold())
        if positions:
            out.append({"group": group, "limit": limit, "positions": positions})

    total_positions = sum(len(v["positions"]) for v in out)
    logger.info(
        "Пороги по группам: групп с нарушениями %d, позиций %d (лимитов в правилах %d).",
        len(out),
        total_positions,
        len(normalized_limits),
    )
    return out


def get_filtered_items(threshold: Optional[int] = None) -> list[dict]:
    """
    Удобная обёртка: читает текущий файл и возвращает отфильтрованные позиции.

    Эквивалентно filter_by_threshold(parse_stock(), threshold).

    :param threshold: Пороговое количество. По умолчанию config.STOCK_THRESHOLD.
    :type threshold: Optional[int]
    :returns: Список товарных позиций с количеством <= threshold.
    :rtype: list[dict]
    """
    all_items = parse_stock()
    return filter_by_threshold(all_items, threshold)


def generate_filtered_partial_xlsx(
    items: list[dict],
    output_path: Optional[str] = None,
) -> str:
    """
    Сохраняет частично отфильтрованные остатки в отдельный файл.

    Формат таблицы тот же, что у :func:`generate_filtered_xlsx` (те же поля
    в элементах ``items``). Путь по умолчанию — :data:`config.FILTERED_PARTIAL_XLSX_PATH`,
    чтобы не перезаписывать глобальный ``filtered.xlsx`` из планировщика.

    :param items: Результат :func:`filter_by_group_thresholds`.
    :type items: list[dict]
    :param output_path: Путь для сохранения. По умолчанию частичный файл в storage.
    :type output_path: Optional[str]
    :returns: Путь к сохранённому xlsx.
    :rtype: str
    :side effects: Создаёт/перезаписывает файл на диске (делегирует
                   :func:`generate_filtered_xlsx`).
    """
    path = output_path or config.FILTERED_PARTIAL_XLSX_PATH
    return generate_filtered_xlsx(items, output_path=path)


def generate_filtered_xlsx(
    items: list[dict],
    output_path: Optional[str] = None,
) -> str:
    """
    Генерирует xlsx-файл из отфильтрованных позиций с группировкой по категориям.

    Каждая категория верхнего уровня (top_category) оформляется как заголовок группы.
    Строки товаров внутри группы имеют outline_level=1 — их можно свернуть/развернуть
    стандартными кнопками Excel. Кнопки управления группой расположены сверху (над группой).
    Строки с остатком <= 1 подсвечиваются красным.

    :param items: Отфильтрованные позиции (``filter_by_threshold`` или
                  :func:`filter_by_group_thresholds` — структура словаря та же).
    :type items: list[dict]
    :param output_path: Путь для сохранения. По умолчанию
                        :data:`config.FILTERED_XLSX_PATH`.
    :type output_path: Optional[str]
    :returns: Абсолютный путь к сохранённому файлу.
    :rtype: str

    :example:
    >>> items = get_filtered_items(threshold=5)
    >>> path = generate_filtered_xlsx(items)
    >>> print(f"Файл готов: {path}, позиций: {len(items)}")
    """
    path = output_path or config.FILTERED_XLSX_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Малый остаток"

    # Кнопки сворачивания групп сверху, а не снизу
    ws.sheet_properties.outlinePr.summaryBelow = False

    # --- Стили ---
    col_header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    col_header_font = Font(bold=True, color="FFFFFF", size=11)

    cat_fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    cat_font = Font(bold=True, color="FFFFFF", size=11)

    row_fill_even = PatternFill(start_color="DEEAF1", end_color="DEEAF1", fill_type="solid")
    row_fill_odd  = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    red_fill      = PatternFill(start_color="FFE0E0", end_color="FFE0E0", fill_type="solid")

    col_widths = [40, 20, 12, 12]
    # Ширина колонки «Наименование» в символах — используется для расчёта высоты строк
    NAME_COL_CHARS = 40

    # --- Строка заголовков колонок ---
    col_headers = ["Наименование", "Бренд", "Количество", "Цена"]
    for col_idx, header in enumerate(col_headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = col_header_font
        cell.fill = col_header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    # --- Группируем по нормализованной верхней категории (как в list_product_groups / частичном фильтре) ---
    def _group_key(it: dict) -> str:
        return normalize_top_category_label(it.get("top_category", ""))

    sorted_items = sorted(
        items,
        key=lambda x: (
            _group_key(x),
            x.get("subcategory", "") or "",
            x.get("brand", "") or "",
        ),
    )

    current_row = 2
    item_counter = 0  # для чередования цвета внутри группы

    for category, group in groupby(sorted_items, key=_group_key):
        group_list = list(group)

        # --- Строка заголовка категории ---
        cat_cell = ws.cell(row=current_row, column=1, value=f"  {category}  ({len(group_list)} поз.)")
        cat_cell.font = cat_font
        cat_cell.fill = cat_fill
        cat_cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[current_row].height = 22

        # Заливаем остальные колонки заголовка категории
        for col_idx in range(2, 5):
            c = ws.cell(row=current_row, column=col_idx)
            c.fill = cat_fill

        current_row += 1
        item_counter = 0

        # --- Строки товаров ---
        for item in group_list:
            qty = item.get("quantity", 0)

            if qty <= 1:
                row_fill = red_fill
            else:
                row_fill = row_fill_even if item_counter % 2 == 0 else row_fill_odd

            row_data = [
                item.get("name", ""),
                item.get("brand", ""),
                qty,
                item.get("price"),
            ]

            for col_idx, value in enumerate(row_data, start=1):
                cell = ws.cell(row=current_row, column=col_idx, value=value)
                cell.fill = row_fill
                cell.alignment = Alignment(
                    vertical="center",
                    wrap_text=(col_idx == 1),
                )

            # Рассчитываем высоту строки по длине названия
            name = item.get("name", "")
            lines = math.ceil(len(name) / NAME_COL_CHARS) if name else 1
            row_height = max(18, lines * 16)

            rd = ws.row_dimensions[current_row]
            rd.outline_level = 1
            rd.height = row_height

            current_row += 1
            item_counter += 1

        # Группировка строк товаров реализована через outline_level=1 выше

    # --- Ширина колонок ---
    for col_idx, width in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Закрепить строку заголовков
    ws.freeze_panes = "A2"

    wb.save(path)
    logger.info("Отфильтрованный xlsx с группировкой сохранён: %s (%d позиций)", path, len(items))
    return path
