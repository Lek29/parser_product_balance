"""
Текст уведомлений о нарушении лимитов по товарным группам
(``docs/fourty_role.md``, этап 5).

Формат: **HTML** (:class:`telegram.constants.ParseMode.HTML`) —
экранирование имён групп и SKU через :func:`html.escape`.
Сообщение — одна строка; при превышении лимита длины список позиций
ужимается с пометкой «и ещё N позиций».
"""

from __future__ import annotations

import html
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Запас под заголовок и служебные строки (лимит Telegram — 4096).
_DEFAULT_MAX_MESSAGE_CHARS: int = 3800
# Нижняя граница числа позиций на группу при итеративном ужатии.
_MIN_POSITIONS_PER_GROUP: int = 5


def format_group_limit_alerts_html(
    violations: list[dict[str, Any]],
    *,
    max_message_chars: int = _DEFAULT_MAX_MESSAGE_CHARS,
) -> Optional[str]:
    """
    Собирает одно HTML-сообщение по списку срабатываний из
    :func:`parser.excel_parser.find_group_limit_violations`.

    Пустой ``violations`` или отсутствие позиций — ``None``
    (отправлять нечего).

    Для каждой группы: заголовок (имя, лимит), маркированный список
    позиций с количеством. Если позиций больше, чем укладывается в
    ``max_message_chars``, число показываемых строк на группу
    уменьшается; при нехватке места добавляется строка
    «… и ещё N позиций». В крайнем случае текст жёстко обрезается
    с пометкой.

    :param violations: Список ``{"group", "limit", "positions"}``.
    :type violations: list[dict[str, Any]]
    :param max_message_chars: Максимальная длина результата (символы Unicode).
    :type max_message_chars: int
    :returns: HTML-строка или ``None``, если срабатываний нет.
    :rtype: Optional[str]

    :example:
    >>> viol = [{"group": "A", "limit": 2,
    ...          "positions": [{"name": "x<y", "quantity": 1}]}]
    >>> fmt = format_group_limit_alerts_html(viol)
    >>> fmt is not None and "&lt;" in fmt
    True
    """
    if not violations:
        return None

    has_any = any(v.get("positions") for v in violations)
    if not has_any:
        return None

    cap = 200
    last_text: Optional[str] = None
    for _ in range(24):
        built = _build_alerts_html(violations, per_group_cap=cap)
        if built is None:
            return None
        last_text = built
        if len(built) <= max_message_chars:
            return built
        if cap <= _MIN_POSITIONS_PER_GROUP:
            break
        cap = max(_MIN_POSITIONS_PER_GROUP, cap // 2)

    if last_text is None:
        return None
    if len(last_text) <= max_message_chars:
        return last_text

    reserve = 80
    if len(last_text) <= max_message_chars + reserve:
        return last_text[:max_message_chars]

    head = last_text[: max_message_chars - reserve]
    tail = "\n\n<i>… текст обрезан (лимит Telegram).</i>"
    out = head + tail
    if len(out) > max_message_chars:
        out = out[:max_message_chars]
    logger.warning(
        "Алерт лимитов: сообщение усечено до %d символов.",
        max_message_chars,
    )
    return out


def _build_alerts_html(
    violations: list[dict[str, Any]],
    *,
    per_group_cap: int,
) -> Optional[str]:
    """
    Собирает HTML без внешней проверки длины.

    :param violations: Срабатывания по группам.
    :type violations: list[dict[str, Any]]
    :param per_group_cap: Максимум позиций на одну группу в теле сообщения.
    :type per_group_cap: int
    :returns: HTML или ``None`` если нечего показывать.
    :rtype: Optional[str]
    """
    blocks: list[str] = []
    preamble = "⚠️ <b>Остатки по лимитам групп</b>\n<i>Позиции с количеством ≤ заданного лимита.</i>\n"

    for v in violations:
        positions = v.get("positions") or []
        if not positions:
            continue
        group_raw = v.get("group", "")
        limit_val = v.get("limit", 0)
        try:
            limit_int = int(limit_val)
        except (TypeError, ValueError):
            limit_int = 0

        group_esc = html.escape(str(group_raw), quote=True)
        shown = positions[: max(0, per_group_cap)]
        omitted = len(positions) - len(shown)

        lines: list[str] = [
            f"<b>Группа:</b> {group_esc}",
            f"<b>Лимит:</b> <code>{limit_int}</code> шт.",
            "",
            "<b>Позиции:</b>",
        ]
        for p in shown:
            name_esc = html.escape(str(p.get("name", "")), quote=True)
            try:
                qty = int(p.get("quantity", 0))
            except (TypeError, ValueError):
                qty = 0
            lines.append(f"• {name_esc} — <code>{qty}</code> шт.")
        if omitted > 0:
            lines.append(f"<i>… и ещё {omitted} поз.</i>")

        blocks.append("\n".join(lines))

    if not blocks:
        return None
    body = "\n\n".join(blocks)
    return preamble + "\n" + body
