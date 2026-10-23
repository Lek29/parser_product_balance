"""
Клавиатуры и кнопки Telegram-бота.

Содержит функции для создания inline-клавиатур и reply-клавиатур,
используемых в боте. Callback-данные кнопок объявлены как константы
для безопасного сравнения в хэндлерах.

Для частичной фильтрации: третья кнопка главного меню,
``groups_reply_keyboard``, ``partial_threshold_inline_keyboard``,
``parse_partial_threshold_callback``. Лимиты по группам (алерты):
четвёртая кнопка меню, ``group_limit_threshold_inline_keyboard``,
``parse_group_limit_callback``.
"""

import re
from typing import Optional, Union

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Callback-идентификаторы кнопок (общий фильтр)
CB_ACTUALIZE = "actualize"
CB_GET_FILTERED = "get_filtered"

# Вход в сценарий частичной фильтрации с главного inline-меню
CB_PARTIAL_START = "partial_start"

# Вход в мастер лимитов по группам (постоянные пороги, docs/fourty_role.md)
CB_GROUP_LIMIT_START = "group_limit_start"

# Текст постоянной кнопки Start (должен точно совпадать
# с фильтром filters.Regex в handlers.py)
START_BUTTON_TEXT = "▶️ Start"


def normalize_reply_button_text(text: str) -> str:
    """
    Нормализует текст reply-кнопки для сравнения с константами.

    Убирает U+FE0F (variation selector у emoji), заменяет неразрывные и прочие
    Unicode-пробелы на обычный пробел, схлопывает пробелы — иначе «✅\\u00a0
    Применить» с клиента не совпадёт с ``PARTIAL_REPLY_APPLY_TEXT``.

    :param text: Сырой текст сообщения или подписи кнопки.
    :type text: str
    :returns: Строка для сравнения с ``PARTIAL_REPLY_*`` и именами групп.
    :rtype: str
    :side effects: Нет.
    """
    s = (text or "").replace("\ufe0f", "")
    for ch in (
        "\u00a0",
        "\u202f",
        "\u2007",
        "\u3000",
        "\t",
        "\n",
        "\r",
        "\u200b",
        "\u200c",
        "\u200d",
    ):
        s = s.replace(ch, " ")
    return " ".join(s.split())


# --- Reply: режим выбора групп (частичная фильтрация) ---
PARTIAL_REPLY_APPLY_TEXT = "✅ Применить"
PARTIAL_REPLY_CANCEL_TEXT = "❌ Отмена"

# Reply-футер в мастере лимитов по группам (выход в главное меню)
LIMIT_REPLY_MENU_TEXT = "✅ В меню"

# Пресеты порога для inline после выбора группы (reply)
PARTIAL_THRESHOLD_PRESETS: tuple[int, ...] = (0, 1, 3, 5, 10)

# Пресеты лимита для уведомлений по группе (inline после выбора группы)
GROUP_LIMIT_PRESETS: tuple[int, ...] = (10, 5, 3)

# Префикс callback выбора порога: ``pft:<индекс_группы>:<порог|x>``
PARTIAL_THRESHOLD_CB_PREFIX = "pft"
_PARTIAL_THRESHOLD_CB_RE = re.compile(r"^pft:(\d+):(\d+|x)$")

# Префикс callback лимита по группе: ``glt:<i>:10|5|3|x|c>`` (c — снять лимит)
GROUP_LIMIT_CB_PREFIX = "glt"
_GROUP_LIMIT_CB_RE = re.compile(r"^glt:(\d+):(10|5|3|x|c)$")

# Две кнопки в ряд для названий групп (компактно и читаемо).
GROUP_BUTTONS_PER_ROW = 2


def parse_partial_threshold_callback(
    callback_data: str,
) -> Optional[tuple[int, Union[int, str]]]:
    """
    Разбирает ``callback_data`` кнопки порога частичной фильтрации.

    Формат строк: ``pft:<i>:<число>`` или ``pft:<i>:x`` (свой порог текстом).

    :param callback_data: Значение из ``CallbackQuery``.
    :type callback_data: str
    :returns: ``(индекс_группы, порог)`` или ``(индекс_группы, \"custom\")``;
              ``None`` если формат не подошёл.
    :rtype: Optional[tuple[int, Union[int, str]]]
    :side effects: Нет (чистая функция).
    """
    if not callback_data:
        return None
    m = _PARTIAL_THRESHOLD_CB_RE.match(callback_data.strip())
    if not m:
        return None
    idx = int(m.group(1))
    raw = m.group(2)
    if raw == "x":
        return (idx, "custom")
    return (idx, int(raw))


def parse_group_limit_callback(
    callback_data: str,
) -> Optional[tuple[int, Union[int, str]]]:
    """
    Разбирает ``callback_data`` кнопки лимита по группе.

    Формат: ``glt:<i>:10|5|3|x|c`` — пресеты, свой лимит текстом, снять лимит.

    :param callback_data: Значение из ``CallbackQuery``.
    :type callback_data: str
    :returns: ``(индекс_группы, порог|\"custom\"|\"clear\")`` или ``None``.
    :rtype: Optional[tuple[int, Union[int, str]]]
    """
    if not callback_data:
        return None
    m = _GROUP_LIMIT_CB_RE.match(callback_data.strip())
    if not m:
        return None
    idx = int(m.group(1))
    raw = m.group(2)
    if raw == "x":
        return (idx, "custom")
    if raw == "c":
        return (idx, "clear")
    return (idx, int(raw))


def start_keyboard() -> ReplyKeyboardMarkup:
    """
    Возвращает reply-клавиатуру с кнопкой «▶️ Start».

    Отображается после успешного ввода пароля.
    Нажатие отправляет текст START_BUTTON_TEXT, который перехватывает
    хэндлер handle_start_button и показывает полное inline-меню.

    :returns: Объект ReplyKeyboardMarkup с одной кнопкой Start.
    :rtype: ReplyKeyboardMarkup

    :example:
    >>> from bot.keyboards import start_keyboard
    >>> markup = start_keyboard()
    >>> await message.reply_text("Нажмите Start", reply_markup=markup)
    """
    keyboard = [[KeyboardButton(START_BUTTON_TEXT)]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def remove_keyboard() -> ReplyKeyboardRemove:
    """
    Возвращает объект для скрытия reply-клавиатуры у пользователя.

    **Резерв:** в текущих потоках бота не вызывается. Предусмотрено для
    сценариев вроде явного сброса авторизации или скрытия reply-клавиатуры без
    смены текстов остальных экранов.

    :returns: Объект ReplyKeyboardRemove.
    :rtype: ReplyKeyboardRemove

    :example:
    >>> from bot.keyboards import remove_keyboard
    >>> await message.reply_text("Клавиатура скрыта",
    ...                          reply_markup=remove_keyboard())
    """
    return ReplyKeyboardRemove()


def main_keyboard() -> InlineKeyboardMarkup:
    """
    Возвращает основную inline-клавиатуру бота с тремя кнопками управления.

    Первые две кнопки — без изменений по смыслу и порядку:
    «Актуализировать остатки», «Получить отфильтрованные остатки».
    Третья — вход в частичную фильтрацию по группам.
    Четвёртая — лимиты по группам для уведомлений
    (см. ``docs/fourty_role.md``).

    :returns: Объект InlineKeyboardMarkup для передачи в reply_markup.
    :rtype: InlineKeyboardMarkup

    :example:
    >>> from bot.keyboards import main_keyboard
    >>> markup = main_keyboard()
    >>> await message.reply_text("Выберите действие:", reply_markup=markup)
    """
    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 Актуализировать остатки",
                callback_data=CB_ACTUALIZE,
            )
        ],
        [
            InlineKeyboardButton(
                "📥 Получить отфильтрованные остатки",
                callback_data=CB_GET_FILTERED,
            )
        ],
        [
            InlineKeyboardButton(
                "🔀 Частичная фильтрация",
                callback_data=CB_PARTIAL_START,
            )
        ],
        [
            InlineKeyboardButton(
                "🎚 Лимиты по группам",
                callback_data=CB_GROUP_LIMIT_START,
            )
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def groups_reply_keyboard(
    group_names: list[str],
    *,
    footer_done_text: str = PARTIAL_REPLY_APPLY_TEXT,
    footer_cancel_text: str = PARTIAL_REPLY_CANCEL_TEXT,
) -> ReplyKeyboardMarkup:
    """
    Reply-клавиатура: кнопки с именами групп и строка «Применить» / «Отмена».

    Группы размещаются по :data:`GROUP_BUTTONS_PER_ROW` в ряд. Внизу всегда
    одна строка с двумя кнопками футера (по умолчанию «Применить» и
    «Отмена» для частичной фильтрации; для лимитов передайте
    ``footer_done_text=LIMIT_REPLY_MENU_TEXT``).

    Пустой ``group_names`` допустим: останутся только служебные кнопки
    (вызывающий код обычно не показывает такую клавиатуру).

    :param group_names: Имена групп (как :func:`list_product_groups`).
    :type group_names: list[str]
    :param footer_done_text: Текст левой кнопки нижнего ряда.
    :type footer_done_text: str
    :param footer_cancel_text: Текст правой кнопки нижнего ряда.
    :type footer_cancel_text: str
    :returns: ReplyKeyboardMarkup для режима частичной фильтрации.
    :rtype: ReplyKeyboardMarkup
    :side effects: Нет (только сборка объекта разметки).
    """
    keyboard: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []

    for name in group_names:
        row.append(KeyboardButton(name))
        if len(row) >= GROUP_BUTTONS_PER_ROW:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append(
        [
            KeyboardButton(footer_done_text),
            KeyboardButton(footer_cancel_text),
        ]
    )

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def partial_threshold_inline_keyboard(
    group_index: int,
) -> InlineKeyboardMarkup:
    """
    Inline-клавиатура пресетов порога и «Свой порог» для группы.

    ``callback_data``: ``pft:<i>:<n|x>`` — см.
    :func:`parse_partial_threshold_callback`.

    :param group_index: Индекс группы в ``UD_PARTIAL_GROUPS_ORDER``.
    :type group_index: int
    :returns: Разметка для сообщения с выбором порога.
    :rtype: InlineKeyboardMarkup
    :side effects: Нет (только сборка объекта разметки).
    """
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []

    for preset in PARTIAL_THRESHOLD_PRESETS:
        cb = f"{PARTIAL_THRESHOLD_CB_PREFIX}:{group_index}:{preset}"
        current.append(
            InlineKeyboardButton(str(preset), callback_data=cb),
        )
        if len(current) >= 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    custom_cb = f"{PARTIAL_THRESHOLD_CB_PREFIX}:{group_index}:x"
    rows.append(
        [
            InlineKeyboardButton("✏️ Свой порог", callback_data=custom_cb),
        ]
    )

    return InlineKeyboardMarkup(rows)


def group_limit_threshold_inline_keyboard(
    group_index: int,
) -> InlineKeyboardMarkup:
    """
    Inline-клавиатура пресетов лимита (10/5/3), «Свой» и «Снять лимит».

    ``callback_data``: ``glt:<i>:<10|5|3|x|c>`` — см.
    :func:`parse_group_limit_callback`.

    :param group_index: Индекс группы в порядке мастера лимитов.
    :type group_index: int
    :returns: Разметка для сообщения с выбором лимита.
    :rtype: InlineKeyboardMarkup
    :side effects: Нет (только сборка объекта разметки).
    """
    row_presets: list[InlineKeyboardButton] = []
    for preset in GROUP_LIMIT_PRESETS:
        cb = f"{GROUP_LIMIT_CB_PREFIX}:{group_index}:{preset}"
        row_presets.append(
            InlineKeyboardButton(str(preset), callback_data=cb),
        )
    custom_cb = f"{GROUP_LIMIT_CB_PREFIX}:{group_index}:x"
    clear_cb = f"{GROUP_LIMIT_CB_PREFIX}:{group_index}:c"
    rows: list[list[InlineKeyboardButton]] = [
        row_presets,
        [InlineKeyboardButton("✏️ Свой лимит", callback_data=custom_cb)],
        [InlineKeyboardButton("🗑 Снять лимит", callback_data=clear_cb)],
    ]
    return InlineKeyboardMarkup(rows)
