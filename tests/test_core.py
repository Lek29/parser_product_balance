"""
Минимальные unit-тесты без сети и Telegram (см. docs/refactoring-plan.md §2.3).
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from parser.excel_parser import (
    TOP_CATEGORY_FALLBACK_LABEL,
    filter_by_group_thresholds,
    generate_filtered_xlsx,
)


def test_filter_by_group_thresholds_basic() -> None:
    """
    Частичный фильтр: только группы из словаря; порог включительно.
    """
    items = [
        {"top_category": "Аксессуары", "quantity": 2, "name": "a"},
        {"top_category": "Мячи", "quantity": 10, "name": "b"},
        {"top_category": "Аксессуары", "quantity": 7, "name": "c"},
    ]
    out = filter_by_group_thresholds(
        items,
        {"Аксессуары": 5, "Мячи": 15},
    )
    assert len(out) == 2
    assert {o["top_category"] for o in out} == {"Аксессуары", "Мячи"}


def test_filter_by_group_thresholds_empty_rules() -> None:
    """Пустой словарь порогов — пустой результат."""
    items = [{"top_category": "X", "quantity": 1}]
    assert filter_by_group_thresholds(items, {}) == []


def test_filter_by_group_thresholds_fallback_group() -> None:
    """Пустой top_category нормализуется в TOP_CATEGORY_FALLBACK_LABEL."""
    items = [{"top_category": "", "quantity": 1}]
    out = filter_by_group_thresholds(items, {TOP_CATEGORY_FALLBACK_LABEL: 5})
    assert len(out) == 1


def test_check_update_available_no_meta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Нет meta.json — обновление «нужно»."""
    import config

    monkeypatch.setattr(config, "META_JSON_PATH", str(tmp_path / "missing.json"))
    from storage.data_store import check_update_available

    assert check_update_available("Mon, 1 Jan 2026 00:00:00 +0000") is True


def test_check_update_available_same_date_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Совпадение строки даты — без обновления."""
    import config

    meta_path = tmp_path / "meta.json"
    date_str = "Mon, 16 Mar 2026 09:00:00 +0800"
    meta_path.write_text(json.dumps({"email_date": date_str}), encoding="utf-8")
    monkeypatch.setattr(config, "META_JSON_PATH", str(meta_path))

    from storage.data_store import check_update_available

    assert check_update_available(date_str) is False


def test_load_items_corrupt_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Повреждённый data.json — пустой список, без исключения (QA-002)."""
    import config

    bad = tmp_path / "data.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_JSON_PATH", str(bad))

    from storage.data_store import load_items

    assert load_items() == []


def test_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    """Атомарная запись читается обратно."""
    p = tmp_path / "t.json"
    from storage.data_store import atomic_write_json

    atomic_write_json(str(p), {"a": 1}, indent=2)
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


def test_generate_filtered_xlsx_empty_top_category_uses_fallback_label(
    tmp_path: Path,
) -> None:
    """
    Пустой ``top_category`` в выгрузке даёт заголовок группы как в частичном фильтре.
    """
    out = tmp_path / "out.xlsx"
    items = [
        {
            "top_category": "",
            "subcategory": "",
            "brand": "B",
            "name": "SKU",
            "quantity": 1,
            "price": 100,
        },
    ]
    path = generate_filtered_xlsx(items, output_path=str(out))
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    header = ws.cell(row=2, column=1).value
    assert header is not None
    assert TOP_CATEGORY_FALLBACK_LABEL in str(header)


def test_authorize_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Повторный authorize для того же chat_id не дублирует запись в auth.json."""
    import config
    import storage.data_store as ds

    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(config, "AUTH_JSON_PATH", str(auth_path))
    ds._auth_ids_cache = None
    ds._auth_cache_mtime = None

    from storage.data_store import authorize

    authorize(999001)
    authorize(999001)

    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert data["authorized"] == [999001]
    assert data["authorized"].count(999001) == 1


def test_read_meta_json_unifies_status_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Один объект meta даёт согласованные поля для get_last_email_date_str и статуса."""
    import config

    meta_path = tmp_path / "meta.json"
    payload = {
        "email_date": "Mon, 1 Jan 2026 12:00:00 +0000",
        "updated_at": "2026-01-01T15:00:00",
        "last_check_date": "2026-01-01",
    }
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config, "META_JSON_PATH", str(meta_path))

    from storage.data_store import get_last_email_date_str, get_status_text, read_meta_json

    m = read_meta_json()
    assert m is not None
    assert m.get("email_date") == payload["email_date"]
    assert get_last_email_date_str() == payload["email_date"]
    assert "01.01.2026" in get_status_text()
