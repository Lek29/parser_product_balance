"""
Календарная неделя Пн–Вс в таймзоне Europe/Moscow.

Используется для политики «свежести» локального снимка остатков (IMAP skip)
и для скрипта очистки папки Stock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def now_moscow(reference: Optional[datetime] = None) -> datetime:
    """
    Возвращает «сейчас» (или ``reference``) в Europe/Moscow.

    :param reference: Произвольный момент; ``None`` — текущее время.
    :type reference: Optional[datetime]
    :returns: Aware-datetime в Europe/Moscow.
    :rtype: datetime
    """
    if reference is None:
        return datetime.now(MOSCOW_TZ)
    if reference.tzinfo is None:
        return reference.replace(tzinfo=MOSCOW_TZ)
    return reference.astimezone(MOSCOW_TZ)


def today_moscow(reference: Optional[datetime] = None) -> date:
    """
    Календарная дата «сегодня» по Москве.

    :param reference: Точка отсчёта; ``None`` — текущее время.
    :type reference: Optional[datetime]
    :returns: Дата в Europe/Moscow.
    :rtype: date
    """
    return now_moscow(reference).date()


def week_monday(d: date) -> date:
    """
    Понедельник календарной недели (ISO: Пн=0 … Вс=6), к которой относится ``d``.

    :param d: Любая дата.
    :type d: date
    :returns: Дата понедельника той же недели.
    :rtype: date
    """
    return d - timedelta(days=d.weekday())


def previous_week_monday(d: date) -> date:
    """
    Понедельник предыдущей календарной недели относительно ``d``.

    :param d: Любая дата.
    :type d: date
    :returns: Пн прошлой недели.
    :rtype: date
    """
    return week_monday(d) - timedelta(days=7)


def same_calendar_week(a: date, b: date) -> bool:
    """
    Проверяет, что две даты лежат в одной календарной неделе Пн–Вс.

    :param a: Первая дата.
    :type a: date
    :param b: Вторая дата.
    :type b: date
    :returns: ``True``, если понедельники недель совпадают.
    :rtype: bool
    """
    return week_monday(a) == week_monday(b)


def parse_meta_date(value: object) -> Optional[date]:
    """
    Разбирает дату из полей meta (``YYYY-MM-DD`` или ISO datetime).

    Naive datetime трактуется как Europe/Moscow; aware — конвертируется в МСК.

    :param value: Строка даты/времени или иной объект (не строка → ``None``).
    :type value: object
    :returns: Календарная дата по Москве или ``None``, если разобрать нельзя.
    :rtype: Optional[date]
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            return date.fromisoformat(raw)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW_TZ)
    else:
        dt = dt.astimezone(MOSCOW_TZ)
    return dt.date()
