"""
Состояние сессии для режимов фильтрации (общий / частичный).

Хранит в ``context.user_data`` режим работы, черновик порогов по группам
(частичная выгрузка), отдельный мастер **лимитов по группам** (роль
``docs/fourty_role.md``) и вспомогательные данные для коротких callback
(индекс группы в упорядоченном списке).

Сброс состояния выполняется при ``/start``, кнопке «▶️ Start», успешной
отправке файла общего фильтра, а также при явном переходе между режимами
(см. хэндлеры). После успешной актуализации с новыми данными сессия
частичного фильтра также сбрасывается.
"""

from __future__ import annotations

import unicodedata
from enum import Enum
from typing import Any, Optional

from bot.keyboards import normalize_reply_button_text

# --- Режим работы бота для пользователя ---


class FilterMode(str, Enum):
    """
    Режим фильтрации: весь документ по одному порогу или по группам.

    :cvar GLOBAL_FILTER: Стандартное меню (актуализация, общий Excel).
    :cvar PARTIAL_FILTER: Частичная фильтрация (reply-группы, inline-пороги).
    :cvar GROUP_LIMIT_SETUP: Установка постоянных лимитов по группам (алерты).
    """

    GLOBAL_FILTER = "global_filter"
    PARTIAL_FILTER = "partial_filter"
    GROUP_LIMIT_SETUP = "group_limit_setup"


# --- Ключи user_data (единая точка, чтобы не опечатываться в хэндлерах) ---
UD_FILTER_MODE: str = "filter_mode"
UD_PARTIAL_THRESHOLDS: str = "partial_thresholds"
UD_PARTIAL_GROUPS_ORDER: str = "partial_groups_order"
UD_PARTIAL_WIZARD_STEP: str = "partial_wizard_step"
UD_PARTIAL_PENDING_GROUP_INDEX: str = "partial_pending_group_index"

# --- Лимиты по группам (постоянное хранилище, не черновик Excel) ---
UD_GL_GROUPS_ORDER: str = "group_limit_groups_order"
UD_GL_WIZARD_STEP: str = "group_limit_wizard_step"
UD_GL_PENDING_GROUP_INDEX: str = "group_limit_pending_group_index"


class PartialWizardStep(str, Enum):
    """
    Шаг мастера частичной фильтрации (reply-группы, выбор порога).

    :cvar IDLE: Черновик не активен или пользователь в глобальном режиме.
    :cvar GROUPS_LIST: Показан список групп (reply-клавиатура).
    :cvar THRESHOLD_FOR_GROUP: Выбрана группа, ожидается порог (inline/текст).
    :cvar AWAITING_THRESHOLD_TEXT: Ожидается ввод своего порога числом в чат.
    """

    IDLE = "idle"
    GROUPS_LIST = "groups_list"
    THRESHOLD_FOR_GROUP = "threshold_for_group"
    AWAITING_THRESHOLD_TEXT = "awaiting_threshold_text"


class GroupLimitWizardStep(str, Enum):
    """
    Шаг мастера лимитов по группам (reply-группы, inline-пресеты / свой ввод).

    :cvar IDLE: Мастер не активен.
    :cvar GROUPS_LIST: Показан список групп (reply-клавиатура).
    :cvar LIMIT_FOR_GROUP: Выбрана группа, показаны inline-кнопки лимита.
    :cvar AWAITING_LIMIT_TEXT: Ожидается ввод своего лимита числом в чат.
    """

    IDLE = "idle"
    GROUPS_LIST = "groups_list"
    LIMIT_FOR_GROUP = "limit_for_group"
    AWAITING_LIMIT_TEXT = "awaiting_limit_text"


def get_filter_mode(context: Any) -> FilterMode:
    """
    Возвращает текущий режим фильтрации; по умолчанию — общий фильтр.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Значение :class:`FilterMode`.
    :rtype: FilterMode
    """
    raw = context.user_data.get(UD_FILTER_MODE)
    if raw is None:
        return FilterMode.GLOBAL_FILTER
    try:
        return FilterMode(raw)
    except ValueError:
        return FilterMode.GLOBAL_FILTER


def set_filter_mode(context: Any, mode: FilterMode) -> None:
    """
    Устанавливает режим фильтрации в ``user_data``.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param mode: Целевой режим.
    :type mode: FilterMode
    :returns: None
    :side effects: Записывает ``UD_FILTER_MODE``.
    """
    context.user_data[UD_FILTER_MODE] = mode.value


def clear_partial_draft(context: Any) -> None:
    """
    Очищает черновик частичной фильтрации (пороги, порядок групп, шаг).

    Режим (:data:`UD_FILTER_MODE`) не меняет — используйте вместе с
    :func:`set_filter_mode` или :func:`reset_session_to_global`.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: None
    :side effects: Удаляет ключи черновика из ``user_data``.
    """
    context.user_data.pop(UD_PARTIAL_THRESHOLDS, None)
    context.user_data.pop(UD_PARTIAL_GROUPS_ORDER, None)
    context.user_data.pop(UD_PARTIAL_WIZARD_STEP, None)
    context.user_data.pop(UD_PARTIAL_PENDING_GROUP_INDEX, None)


def clear_group_limit_session(context: Any) -> None:
    """
    Очищает состояние мастера лимитов по группам.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: None
    :side effects: Удаляет ключи лимитов из ``user_data``.
    """
    context.user_data.pop(UD_GL_GROUPS_ORDER, None)
    context.user_data.pop(UD_GL_WIZARD_STEP, None)
    context.user_data.pop(UD_GL_PENDING_GROUP_INDEX, None)


def reset_session_to_global(context: Any) -> None:
    """
    Сбрасывает сессию в «общий фильтр» и очищает черновики режимов.

    Вызывать при ``/start``, кнопке Start, после успешной отправки файла
    и при выходе из сценариев частичной фильтрации или лимитов по группам.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: None
    :side effects: Режим GLOBAL_FILTER, очистка черновиков.
    """
    set_filter_mode(context, FilterMode.GLOBAL_FILTER)
    clear_partial_draft(context)
    clear_group_limit_session(context)


def init_partial_session(context: Any, groups: list[str]) -> None:
    """
    Инициализирует частичную фильтрацию: режим, порядок групп, пороги {}.

    Список ``groups`` должен совпадать с reply-клавиатурой и с
    :func:`group_name_by_index` / :func:`group_index_by_reply_label`.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param groups: Имена групп (см. ``list_product_groups``).
    :type groups: list[str]
    :returns: None
    :side effects: Режим PARTIAL, порядок групп, шаг GROUPS_LIST.
    """
    clear_group_limit_session(context)
    set_filter_mode(context, FilterMode.PARTIAL_FILTER)
    context.user_data[UD_PARTIAL_GROUPS_ORDER] = list(groups)
    context.user_data[UD_PARTIAL_THRESHOLDS] = {}
    context.user_data[UD_PARTIAL_WIZARD_STEP] = PartialWizardStep.GROUPS_LIST.value
    context.user_data.pop(UD_PARTIAL_PENDING_GROUP_INDEX, None)


def init_group_limit_session(context: Any, groups: list[str]) -> None:
    """
    Инициализирует мастер лимитов по группам (постоянные пороги в ``storage``).

    Черновик частичной фильтрации очищается, чтобы режимы не смешивались.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param groups: Имена групп (см. ``list_product_groups``).
    :type groups: list[str]
    :returns: None
    :side effects: Режим GROUP_LIMIT_SETUP, порядок групп, шаг GROUPS_LIST.
    """
    clear_partial_draft(context)
    set_filter_mode(context, FilterMode.GROUP_LIMIT_SETUP)
    context.user_data[UD_GL_GROUPS_ORDER] = list(groups)
    context.user_data[UD_GL_WIZARD_STEP] = GroupLimitWizardStep.GROUPS_LIST.value
    context.user_data.pop(UD_GL_PENDING_GROUP_INDEX, None)


def get_gl_groups_order(context: Any) -> list[str]:
    """
    Упорядоченный список групп для мастера лимитов.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Копия списка имён или пустой список.
    :rtype: list[str]
    """
    order = context.user_data.get(UD_GL_GROUPS_ORDER)
    if not isinstance(order, list):
        return []
    return list(order)


def gl_group_index_by_reply_label(context: Any, label: str) -> Optional[int]:
    """
    Индекс группы по тексту reply-кнопки (мастер лимитов).

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param label: Текст сообщения (имя группы).
    :type label: str
    :returns: Индекс или ``None``.
    :rtype: Optional[int]
    """
    key = _group_reply_match_key(label or "")
    if not key:
        return None
    order = get_gl_groups_order(context)
    for i, g in enumerate(order):
        if _group_reply_match_key(g) == key:
            return i
    return None


def gl_group_name_by_index(context: Any, index: int) -> Optional[str]:
    """
    Имя группы по индексу (мастер лимитов) или ``None``.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param index: Индекс из callback.
    :type index: int
    :returns: Имя группы или ``None``.
    :rtype: Optional[str]
    """
    order = get_gl_groups_order(context)
    if index < 0 or index >= len(order):
        return None
    return order[index]


def set_gl_wizard_step(context: Any, step: GroupLimitWizardStep) -> None:
    """
    Устанавливает шаг мастера лимитов по группам.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param step: Шаг из :class:`GroupLimitWizardStep`.
    :type step: GroupLimitWizardStep
    :returns: None
    """
    context.user_data[UD_GL_WIZARD_STEP] = step.value


def get_gl_wizard_step(context: Any) -> GroupLimitWizardStep:
    """
    Возвращает шаг мастера лимитов; при неизвестном значении — ``IDLE``.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Текущий шаг.
    :rtype: GroupLimitWizardStep
    """
    raw = context.user_data.get(UD_GL_WIZARD_STEP)
    if raw is None:
        return GroupLimitWizardStep.IDLE
    try:
        return GroupLimitWizardStep(raw)
    except ValueError:
        return GroupLimitWizardStep.IDLE


def set_gl_pending_group_index(context: Any, index: int) -> None:
    """
    Сохраняет индекс группы при запросе «свой лимит» (ввод числа в чат).

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param index: Индекс в :func:`get_gl_groups_order`.
    :type index: int
    :returns: None
    """
    context.user_data[UD_GL_PENDING_GROUP_INDEX] = index


def get_gl_pending_group_index(context: Any) -> Optional[int]:
    """
    Индекс группы, для которой ожидается ввод лимита текстом.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Индекс или ``None``.
    :rtype: Optional[int]
    """
    raw = context.user_data.get(UD_GL_PENDING_GROUP_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def clear_gl_pending_group_index(context: Any) -> None:
    """
    Сбрасывает ожидание ввода лимита текстом.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: None
    """
    context.user_data.pop(UD_GL_PENDING_GROUP_INDEX, None)


def get_partial_thresholds(context: Any) -> dict[str, int]:
    """
    Возвращает черновик порогов по имени группы (копия словаря).

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: ``группа -> порог``; пустой dict, если не инициализировано.
    :rtype: dict[str, int]
    """
    data = context.user_data.get(UD_PARTIAL_THRESHOLDS)
    if not isinstance(data, dict):
        return {}
    return dict(data)


def set_partial_threshold(
    context: Any,
    group_name: str,
    threshold: int,
) -> None:
    """
    Записывает порог для группы в черновик.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param group_name: Имя группы (как в списке групп).
    :type group_name: str
    :param threshold: Порог количества включительно.
    :type threshold: int
    :returns: None
    :side effects: Обновляет ``UD_PARTIAL_THRESHOLDS``.
    """
    thresholds = context.user_data.get(UD_PARTIAL_THRESHOLDS)
    if not isinstance(thresholds, dict):
        thresholds = {}
        context.user_data[UD_PARTIAL_THRESHOLDS] = thresholds
    thresholds[group_name] = threshold


def get_groups_order(context: Any) -> list[str]:
    """
    Упорядоченный список групп текущей сессии частичной фильтрации.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Копия списка имён или пустой список.
    :rtype: list[str]
    """
    order = context.user_data.get(UD_PARTIAL_GROUPS_ORDER)
    if not isinstance(order, list):
        return []
    return list(order)


def _group_reply_match_key(label: str) -> str:
    """
    Ключ для сравнения имени группы с текстом reply-кнопки.

    NFKC убирает совместимые варианты символов; ``casefold`` — для латиницы
    (например ВЕЛО / вело).

    :param label: Имя группы или текст с клавиатуры.
    :type label: str
    :returns: Нормализованная строка для сравнения.
    :rtype: str
    :side effects: Нет.
    """
    t = normalize_reply_button_text(label or "")
    return unicodedata.normalize("NFKC", t).casefold()


def group_index_by_reply_label(context: Any, label: str) -> Optional[int]:
    """
    Находит индекс группы по тексту reply-кнопки (с нормализацией Unicode).

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param label: Текст сообщения (имя группы).
    :type label: str
    :returns: Индекс в порядке групп или ``None``.
    :rtype: Optional[int]
    :side effects: Нет (чтение ``user_data``).
    """
    key = _group_reply_match_key(label)
    if not key:
        return None
    order = get_groups_order(context)
    for i, g in enumerate(order):
        if _group_reply_match_key(g) == key:
            return i
    return None


def is_known_product_group_reply_text(label: str) -> bool:
    """
    Проверяет, совпадает ли текст сообщения с именем группы из
    ``current.xlsx``.

    Нужно, чтобы отличить тап по старой reply-клавиатуре групп от обычного
    текста, когда режим уже не ``PARTIAL_FILTER``.

    :param label: Текст входящего сообщения (как с reply-кнопки).
    :type label: str
    :returns: True если такая группа есть в актуальной таблице.
    :rtype: bool
    :side effects: Читает xlsx через
                   :func:`parser.excel_parser.list_product_groups`.
    """
    from parser.excel_parser import list_product_groups

    key = _group_reply_match_key(label)
    if not key:
        return False
    try:
        names = list_product_groups()
    except Exception:
        return False
    for g in names:
        if _group_reply_match_key(g) == key:
            return True
    return False


def set_wizard_step(context: Any, step: PartialWizardStep) -> None:
    """
    Устанавливает текущий шаг мастера частичной фильтрации.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param step: Шаг из :class:`PartialWizardStep`.
    :type step: PartialWizardStep
    :returns: None
    :side effects: Записывает ``UD_PARTIAL_WIZARD_STEP``.
    """
    context.user_data[UD_PARTIAL_WIZARD_STEP] = step.value


def get_wizard_step(context: Any) -> PartialWizardStep:
    """
    Возвращает шаг мастера; если не распознано — ``IDLE``.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Текущий шаг.
    :rtype: PartialWizardStep
    """
    raw = context.user_data.get(UD_PARTIAL_WIZARD_STEP)
    if raw is None:
        return PartialWizardStep.IDLE
    try:
        return PartialWizardStep(raw)
    except ValueError:
        return PartialWizardStep.IDLE


def set_pending_group_index(context: Any, index: int) -> None:
    """
    Сохраняет индекс группы при запросе «свой порог» (ввод числа в чат).

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param index: Индекс в :func:`get_groups_order`.
    :type index: int
    :returns: None
    :side effects: Записывает ``UD_PARTIAL_PENDING_GROUP_INDEX``.
    """
    context.user_data[UD_PARTIAL_PENDING_GROUP_INDEX] = index


def get_pending_group_index(context: Any) -> Optional[int]:
    """
    Возвращает индекс группы, для которой ожидается текстовый порог, или None.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: Индекс или ``None``.
    :rtype: Optional[int]
    """
    raw = context.user_data.get(UD_PARTIAL_PENDING_GROUP_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def clear_pending_group_index(context: Any) -> None:
    """
    Сбрасывает ожидание ввода порога текстом.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :returns: None
    """
    context.user_data.pop(UD_PARTIAL_PENDING_GROUP_INDEX, None)


def group_name_by_index(context: Any, index: int) -> Optional[str]:
    """
    Имя группы по индексу из сохранённого порядка или ``None``.

    :param context: Контекст python-telegram-bot.
    :type context: Any
    :param index: Индекс из callback (неотрицательный).
    :type index: int
    :returns: Имя группы или ``None`` при выходе за границы.
    :rtype: Optional[str]
    """
    order = get_groups_order(context)
    if index < 0 or index >= len(order):
        return None
    return order[index]
