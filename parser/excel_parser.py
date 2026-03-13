"""
Модуль парсинга Excel-таблицы с остатками товаров.

Читает storage/current.xlsx, определяет иерархию товаров по отступу ячеек (indent),
фильтрует позиции с количеством <= STOCK_THRESHOLD и формирует структурированный список.
Также умеет генерировать отфильтрованный .xlsx для отправки пользователю.

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
from typing import Optional

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

    for row_idx, row in enumerate(ws.iter_rows(min_row=HEADER_ROWS + 1), start=HEADER_ROWS + 1):
        cell_name = row[0]  # колонка A
        cell_qty = row[3]   # колонка D
        cell_price = row[4] # колонка E

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

    :param items: Список отфильтрованных товарных позиций (результат filter_by_threshold).
    :type items: list[dict]
    :param output_path: Путь для сохранения файла. По умолчанию storage/filtered.xlsx.
    :type output_path: Optional[str]
    :returns: Абсолютный путь к сохранённому файлу.
    :rtype: str

    :example:
    >>> items = get_filtered_items(threshold=5)
    >>> path = generate_filtered_xlsx(items)
    >>> print(f"Файл готов: {path}, позиций: {len(items)}")
    """
    path = output_path or os.path.join(config.STORAGE_DIR, "filtered.xlsx")
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

    # --- Группируем по top_category ---
    sorted_items = sorted(items, key=lambda x: (x.get("top_category", ""), x.get("subcategory", ""), x.get("brand", "")))

    current_row = 2
    item_counter = 0  # для чередования цвета внутри группы

    for category, group in groupby(sorted_items, key=lambda x: x.get("top_category", "Без категории")):
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
