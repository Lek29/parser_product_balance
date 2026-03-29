"""
Пайплайн обработки остатков после успешной загрузки current.xlsx.

Хэндлер актуализации и планировщик вызывают одну функцию — без дублирования.
"""

from __future__ import annotations

from parser.excel_parser import (
    filter_by_threshold,
    generate_filtered_xlsx,
    parse_stock,
)
from storage.data_store import save_items


def run_parse_and_save_filtered() -> tuple[int, int]:
    """
    Парсит ``storage/current.xlsx``, пишет ``data.json`` и глобальный ``filtered.xlsx``.

    Файл остатков уже должен быть на месте (после успешного
    :func:`parser.mail_fetcher.fetch_and_save`).

    Порядок намеренно последователен: сначала полный снимок в JSON, затем
    фильтрация и запись ``filtered.xlsx``, чтобы при сбое генерации Excel
    в ``data.json`` уже лежал актуальный полный список (удобно для отладки).

    :returns: Кортеж ``(число всех позиций, число позиций с малым остатком)``.
    :rtype: tuple[int, int]
    :side effects: Обновляет ``data.json`` и ``filtered.xlsx`` в ``storage/``.
    """
    all_items = parse_stock()
    save_items(all_items)
    filtered = filter_by_threshold(all_items)
    generate_filtered_xlsx(filtered)
    return len(all_items), len(filtered)
