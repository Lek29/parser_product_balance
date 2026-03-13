"""
Клавиатуры и кнопки Telegram-бота.

Содержит функции для создания inline-клавиатур и reply-клавиатур,
используемых в боте. Callback-данные кнопок объявлены как константы
для безопасного сравнения в хэндлерах.
"""

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Callback-идентификаторы кнопок
CB_ACTUALIZE = "actualize"
CB_GET_FILTERED = "get_filtered"

# Текст постоянной кнопки Start (должен точно совпадать
# с фильтром filters.Regex в handlers.py)
START_BUTTON_TEXT = "▶️ Start"


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
    return ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=False
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    """
    Возвращает объект для скрытия reply-клавиатуры у пользователя.

    Используется при необходимости убрать кнопку Start
    (например, при сбросе авторизации).

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
    Возвращает основную inline-клавиатуру бота с двумя кнопками управления.

    Кнопки:
    - «🔄 Актуализировать остатки» — проверка и загрузка новых данных с почты.
    - «📥 Получить отфильтрованные остатки» — Excel-файл с товарами ниже порога.

    :returns: Объект InlineKeyboardMarkup для передачи в reply_markup.
    :rtype: InlineKeyboardMarkup

    :example:
    >>> from bot.keyboards import main_keyboard
    >>> markup = main_keyboard()
    >>> await message.reply_text("Выберите действие:", reply_markup=markup)
    """
    keyboard = [
        [InlineKeyboardButton(
            "🔄 Актуализировать остатки",
            callback_data=CB_ACTUALIZE,
        )],
        [InlineKeyboardButton(
            "📥 Получить отфильтрованные остатки",
            callback_data=CB_GET_FILTERED,
        )],
    ]
    return InlineKeyboardMarkup(keyboard)
