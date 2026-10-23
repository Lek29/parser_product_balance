"""
Тесты выбора вложения «Остатки от …» без IMAP (см. docs/invalid_file.md).
"""

from __future__ import annotations

import email
from datetime import date, datetime

import pytest

import config


def _make_candidate(
    filename: str,
    stock_date: date,
    parsed_fn: date | None,
    email_dt: datetime,
    msg_id: bytes = b"1",
) -> object:
    """Собирает ``_StockArchiveCandidate`` с минимальным фиктивным ``msg``."""
    from parser.mail_fetcher import _StockArchiveCandidate

    msg = email.message.EmailMessage()
    msg["Date"] = "Mon, 1 Jan 2026 00:00:00 +0000"
    return _StockArchiveCandidate(
        msg=msg,
        filename=filename,
        email_date=msg["Date"],
        msg_id=msg_id,
        stock_date=stock_date,
        parsed_filename_date=parsed_fn,
        email_dt=email_dt,
    )


def test_pick_best_stock_candidate_max_date() -> None:
    """Несколько кандидатов — выбирается максимальная дата в имени."""
    from parser.mail_fetcher import _pick_best_stock_candidate

    base = datetime(2026, 1, 15, 12, 0, 0)
    c_old = _make_candidate(
        "Остатки от 01.01.2026.zip",
        date(2026, 1, 1),
        date(2026, 1, 1),
        base,
        b"10",
    )
    c_new = _make_candidate(
        "Остатки от 31.03.2026.zip",
        date(2026, 3, 31),
        date(2026, 3, 31),
        base,
        b"20",
    )
    best = _pick_best_stock_candidate([c_old, c_new])
    assert best is not None
    assert best.filename == "Остатки от 31.03.2026.zip"
    assert best.stock_date == date(2026, 3, 31)


def test_pick_best_same_stock_date_newer_email() -> None:
    """При одинаковой дате в имени — более новое письмо по ``email_dt``."""
    from parser.mail_fetcher import _pick_best_stock_candidate

    c1 = _make_candidate(
        "Остатки от 2026-03-15.zip",
        date(2026, 3, 15),
        date(2026, 3, 15),
        datetime(2026, 3, 15, 8, 0, 0),
        b"1",
    )
    c2 = _make_candidate(
        "Остатки от 2026-03-15.zip",
        date(2026, 3, 15),
        date(2026, 3, 15),
        datetime(2026, 3, 16, 8, 0, 0),
        b"2",
    )
    best = _pick_best_stock_candidate([c1, c2])
    assert best is not None
    assert best.email_dt == datetime(2026, 3, 16, 8, 0, 0)


def test_filename_prefix_filters_sales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Продажи…» не считаются остатками; «Остатки…» — да."""
    monkeypatch.setattr(config, "MAIL_STOCK_ARCHIVE_PREFIX", "Остатки от ")

    from parser.mail_fetcher import _filename_has_stock_prefix

    assert _filename_has_stock_prefix("Остатки от 01.01.2026.zip") is True
    assert _filename_has_stock_prefix("остатки от 01.01.2026.zip") is True
    sales = "Продажи за предыдущий день.zip"
    assert _filename_has_stock_prefix(sales) is False


def test_parse_date_iso_and_ddmm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Разбор обоих допустимых шаблонов даты после префикса."""
    monkeypatch.setattr(config, "MAIL_STOCK_ARCHIVE_PREFIX", "Остатки от ")

    from parser.mail_fetcher import _parse_date_after_stock_prefix

    assert _parse_date_after_stock_prefix("Остатки от 31.03.2026.zip") == date(
        2026, 3, 31
    )
    assert _parse_date_after_stock_prefix("Остатки от 2026-03-31.zip") == date(
        2026, 3, 31
    )


def test_effective_stock_date_rejects_bad_without_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нет даты в имени и пустой Date — кандидат не использовать."""
    monkeypatch.setattr(config, "MAIL_STOCK_ARCHIVE_PREFIX", "Остатки от ")

    from parser.mail_fetcher import _effective_stock_date

    assert (
        _effective_stock_date("Остатки от unknown.zip", "") is None
    )


def test_imap_fetch_payload_tuple_ok() -> None:
    """Нормальный FETCH с literal — возвращает bytes payload."""
    from parser.mail_fetcher import _imap_fetch_payload

    payload = b"From: a@b.c\r\n\r\n"
    data = [(b"1 (BODY[HEADER] {18}", payload), b")"]
    assert _imap_fetch_payload(data) == payload


def test_imap_fetch_payload_bare_bytes_is_none() -> None:
    """Ответ без literal (голые bytes) — None, не int через индексацию."""
    from parser.mail_fetcher import _imap_fetch_payload

    # Раньше data[0][1] давал int и message_from_bytes падал с decode.
    data = [b"1 (BODY[HEADER] NIL)"]
    assert _imap_fetch_payload(data) is None
    assert isinstance(data[0][1], int)


def test_imap_fetch_payload_empty_and_bad() -> None:
    """Пустой/битый ответ FETCH — None."""
    from parser.mail_fetcher import _imap_fetch_payload

    assert _imap_fetch_payload(None) is None
    assert _imap_fetch_payload([]) is None
    assert _imap_fetch_payload([None]) is None
    assert _imap_fetch_payload([(b"meta", 42)]) is None
    assert _imap_fetch_payload([(b"meta",)]) is None
