"""
Хэндлеры Telegram-бота.

Обрабатывает команды и нажатия inline-кнопок:
- /start — приветствие; если не авторизован — запрашивает пароль
- handle_password — проверяет введённый пароль и авторизует пользователя
- CB_ACTUALIZE — проверка почты и обновление остатков (только для авторизованных)
- CB_GET_FILTERED — генерация и отправка отфильтрованного Excel-файла (только для авторизованных)

Все хэндлеры асинхронные (python-telegram-bot v20+).
Ошибки перехватываются и сообщаются пользователю без падения бота.
Неавторизованные пользователи не получают доступ к функциям бота.

Защита от брутфорса:
- После MAX_AUTH_ATTEMPTS неверных попыток chat_id блокируется на AUTH_LOCKOUT_SECONDS.
- Счётчики хранятся в памяти (сбрасываются при перезапуске бота).
"""

import hmac
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from bot.keyboards import CB_ACTUALIZE, CB_GET_FILTERED, START_BUTTON_TEXT, main_keyboard, remove_keyboard, start_keyboard
from storage.data_store import authorize, get_status_text, is_authorized

logger = logging.getLogger(__name__)

# Текст запроса пароля
_ASK_PASSWORD_TEXT = (
    "🔒 *Доступ закрыт*\n\n"
    "Введите пароль для входа:"
)

# Текст для неавторизованных при нажатии кнопок
_NOT_AUTH_TEXT = (
    "🔒 Нет доступа.\n\n"
    "Отправьте /start и введите пароль."
)

# --- Защита от брутфорса ---
# Количество допустимых неверных попыток до блокировки
MAX_AUTH_ATTEMPTS: int = 5
# Длительность блокировки в секундах (5 минут)
AUTH_LOCKOUT_SECONDS: int = 300

# chat_id → количество неверных попыток подряд
_failed_attempts: dict[int, int] = {}
# chat_id → timestamp (time.monotonic) до которого заблокирован
_lockout_until: dict[int, float] = {}


def _check_auth(chat_id: int) -> bool:
    """
    Вспомогательная проверка авторизации пользователя по chat_id.

    :param chat_id: Telegram chat_id проверяемого пользователя.
    :type chat_id: int
    :returns: True если пользователь авторизован.
    :rtype: bool
    """
    return is_authorized(chat_id)


def _is_locked_out(chat_id: int) -> bool:
    """
    Проверяет, заблокирован ли пользователь из-за превышения попыток ввода пароля.

    Блокировка снимается автоматически по истечении AUTH_LOCKOUT_SECONDS.

    :param chat_id: Telegram chat_id пользователя.
    :type chat_id: int
    :returns: True если пользователь сейчас заблокирован.
    :rtype: bool
    """
    until = _lockout_until.get(chat_id)
    if until is None:
        return False
    if time.monotonic() >= until:
        _lockout_until.pop(chat_id, None)
        _failed_attempts.pop(chat_id, None)
        return False
    return True


def _record_failed_attempt(chat_id: int) -> int:
    """
    Фиксирует неудачную попытку ввода пароля и при необходимости устанавливает блокировку.

    После MAX_AUTH_ATTEMPTS неверных попыток подряд устанавливает блокировку
    на AUTH_LOCKOUT_SECONDS секунд.

    :param chat_id: Telegram chat_id пользователя.
    :type chat_id: int
    :returns: Текущее количество неудачных попыток после регистрации.
    :rtype: int
    :side effects: Обновляет _failed_attempts и _lockout_until.
    """
    count = _failed_attempts.get(chat_id, 0) + 1
    _failed_attempts[chat_id] = count
    if count >= MAX_AUTH_ATTEMPTS:
        _lockout_until[chat_id] = time.monotonic() + AUTH_LOCKOUT_SECONDS
        logger.warning(
            "Пользователь %d заблокирован на %d сек. после %d неверных попыток.",
            chat_id, AUTH_LOCKOUT_SECONDS, count,
        )
    return count


def _reset_failed_attempts(chat_id: int) -> None:
    """
    Сбрасывает счётчик неудачных попыток после успешной авторизации.

    :param chat_id: Telegram chat_id пользователя.
    :type chat_id: int
    :returns: None
    :side effects: Очищает записи в _failed_attempts и _lockout_until.
    """
    _failed_attempts.pop(chat_id, None)
    _lockout_until.pop(chat_id, None)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хэндлер команды /start.

    Если пользователь авторизован — показывает статус и меню.
    Если не авторизован — запрашивает пароль.

    :param update: Входящий Telegram Update.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    chat_id = update.effective_chat.id

    if not _check_auth(chat_id):
        await update.message.reply_text(
            _ASK_PASSWORD_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = get_status_text()
    text = (
        "👋 *Парсер остатков*\n\n"
        f"📊 {status}\n\n"
        f"🎚 Порог отображения: `<= {config.STOCK_THRESHOLD}` шт.\n\n"
        "Выберите действие:"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )


async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хэндлер текстовых сообщений для проверки пароля.

    Срабатывает на любое текстовое сообщение от неавторизованного пользователя.
    Если текст совпадает с BOT_PASSWORD — авторизует и показывает меню.
    Если не совпадает — фиксирует неудачную попытку.
    После MAX_AUTH_ATTEMPTS неверных попыток блокирует пользователя на AUTH_LOCKOUT_SECONDS.
    Авторизованным пользователям не отвечает (их сообщения игнорируются).

    Сравнение пароля выполняется через hmac.compare_digest() для защиты от timing-атак.

    :param update: Входящий Telegram Update с текстовым сообщением.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    chat_id = update.effective_chat.id

    if _check_auth(chat_id):
        return

    # Проверка блокировки
    if _is_locked_out(chat_id):
        until = _lockout_until.get(chat_id, 0)
        remaining = max(0, int(until - time.monotonic()))
        await update.message.reply_text(
            f"🚫 Слишком много неверных попыток.\n\n"
            f"Повторите через {remaining} сек."
        )
        return

    entered = (update.message.text or "").strip()

    # hmac.compare_digest требует одинаковый тип (str); работает за постоянное время
    password_correct = hmac.compare_digest(entered, config.BOT_PASSWORD)

    if password_correct:
        _reset_failed_attempts(chat_id)
        authorize(chat_id)
        await update.message.reply_text(
            "✅ *Доступ открыт!*\n\nНажмите кнопку ниже для запуска.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=start_keyboard(),
        )
        logger.info("Пользователь %d успешно авторизован через пароль.", chat_id)
    else:
        count = _record_failed_attempt(chat_id)
        remaining_attempts = max(0, MAX_AUTH_ATTEMPTS - count)
        if remaining_attempts > 0:
            await update.message.reply_text(
                f"❌ Неверный пароль. "
                f"Осталось попыток: {remaining_attempts}."
            )
        else:
            await update.message.reply_text(
                f"🚫 Слишком много неверных попыток.\n\n"
                f"Повторите через {AUTH_LOCKOUT_SECONDS} сек."
            )
        logger.warning(
            "Неверный пароль от пользователя %d (попытка %d/%d).",
            chat_id, count, MAX_AUTH_ATTEMPTS,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хэндлер команды /help.

    Доступен без авторизации. Отправляет справку по боту: назначение,
    список команд и описание inline-кнопок.

    :param update: Входящий Telegram Update.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    text = (
        "ℹ️ *Парсер остатков — справка*\n\n"
        "Бот получает актуальные остатки товаров с почты и позволяет "
        "фильтровать позиции с малым запасом.\n\n"
        "*Команды:*\n"
        "/start — запустить бота / открыть главное меню\n"
        "/help — показать эту справку\n\n"
        "*Кнопки меню:*\n"
        "🔄 *Актуализировать остатки* — проверить почту и обновить данные\n"
        "📥 *Получить отфильтрованные остатки* — скачать Excel с позициями "
        f"≤ `{config.STOCK_THRESHOLD}` шт.\n\n"
        "🔒 Для работы с ботом требуется авторизация по паролю."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хэндлер нажатия reply-кнопки «▶️ Start».

    Срабатывает когда авторизованный пользователь нажимает постоянную
    кнопку Start. Показывает статус и основное inline-меню — идентично
    поведению команды /start для авторизованного пользователя.
    Если пользователь не авторизован — запрашивает пароль.

    :param update: Входящий Telegram Update с текстовым сообщением «▶️ Start».
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    chat_id = update.effective_chat.id

    if not _check_auth(chat_id):
        await update.message.reply_text(
            _ASK_PASSWORD_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=remove_keyboard(),
        )
        return

    status = get_status_text()
    text = (
        "👋 *Парсер остатков*\n\n"
        f"📊 {status}\n\n"
        f"🎚 Порог отображения: `<= {config.STOCK_THRESHOLD}` шт.\n\n"
        "Выберите действие:"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )


async def cb_actualize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хэндлер кнопки «Актуализировать остатки».

    Доступен только авторизованным пользователям.
    Подключается к почте, сравнивает дату последнего письма с сохранённой
    в meta.json. Если есть новые данные — скачивает, парсит, сохраняет.
    Сообщает пользователю результат.

    :param update: Входящий Telegram Update с callback_query.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    query = update.callback_query
    await query.answer()

    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    await query.edit_message_text("⏳ Проверяю почту...")

    try:
        from parser.mail_fetcher import fetch_and_save
        updated = fetch_and_save()

        # None — IMAP не вызывался, сегодня уже проверялось
        if updated is None:
            status = get_status_text()
            from storage.data_store import get_last_check_date
            check_date = get_last_check_date() or "сегодня"
            await query.edit_message_text(
                f"✅ Сегодня ({check_date}) уже проверялось.\n\n📊 {status}",
                reply_markup=main_keyboard(),
            )
            return

        # False — IMAP проверен, нового письма нет
        if updated is False:
            status = get_status_text()
            await query.edit_message_text(
                f"✅ Проверено. Новых данных нет.\n\n📊 {status}",
                reply_markup=main_keyboard(),
            )
            return

        from parser.excel_parser import filter_by_threshold, generate_filtered_xlsx, parse_stock
        from storage.data_store import save_items

        await query.edit_message_text("⏳ Обрабатываю таблицу...")

        all_items = parse_stock()
        save_items(all_items)

        filtered = filter_by_threshold(all_items)
        generate_filtered_xlsx(filtered)

        status = get_status_text()
        await query.edit_message_text(
            f"✅ *Остатки обновлены!*\n\n"
            f"📊 {status}\n"
            f"📦 Всего позиций: `{len(all_items)}`\n"
            f"⚠️ Малый остаток (≤ {config.STOCK_THRESHOLD} шт.): `{len(filtered)}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )

    except Exception as e:
        logger.error("Ошибка при актуализации: %s", e, exc_info=True)
        await query.edit_message_text(
            "❌ Произошла ошибка при обновлении данных.\n\nПопробуйте позже.",
            reply_markup=main_keyboard(),
        )


async def cb_get_filtered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хэндлер кнопки «Получить отфильтрованные остатки».

    Доступен только авторизованным пользователям.
    Генерирует xlsx-файл с товарами, количество которых <= STOCK_THRESHOLD,
    и отправляет его пользователю как документ (открывается прямо в Telegram).

    :param update: Входящий Telegram Update с callback_query.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    query = update.callback_query
    await query.answer()

    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    await query.edit_message_text("⏳ Формирую файл...")

    try:
        from parser.excel_parser import generate_filtered_xlsx, get_filtered_items

        filtered = get_filtered_items()

        if not filtered:
            await query.edit_message_text(
                f"📭 Товаров с остатком ≤ {config.STOCK_THRESHOLD} шт. не найдено.\n\n"
                "Попробуйте сначала актуализировать остатки.",
                reply_markup=main_keyboard(),
            )
            return

        xlsx_path = generate_filtered_xlsx(filtered)

        status = get_status_text()
        caption = (
            f"📊 {status}\n"
            f"⚠️ Позиций с малым остатком: *{len(filtered)}*"
        )

        await query.edit_message_text("📤 Отправляю файл...")

        with open(xlsx_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename="остатки_малый_запас.xlsx",
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                write_timeout=120,
                read_timeout=60,
            )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Выберите действие:",
            reply_markup=main_keyboard(),
        )

    except FileNotFoundError:
        await query.edit_message_text(
            "❌ Файл остатков не найден.\n\nСначала нажмите «Актуализировать остатки».",
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        logger.error("Ошибка при отправке файла: %s", e, exc_info=True)
        await query.edit_message_text(
            "❌ Произошла ошибка при формировании файла.\n\nПопробуйте позже.",
            reply_markup=main_keyboard(),
        )


def register_handlers(app: Application) -> None:
    """
    Регистрирует все хэндлеры команд, callback-кнопок и текстовых сообщений.

    Порядок регистрации важен:
    1. Команды (/start, /help) — первыми, чтобы telegram не путал с текстом.
    2. Callback-кнопки inline-клавиатуры.
    3. MessageHandler для reply-кнопки «▶️ Start» — до общего хэндлера пароля,
       иначе текст «▶️ Start» попадёт в handle_password.
    4. MessageHandler для пароля — последним, ловит все остальные текстовые сообщения.

    :param app: Экземпляр Application из python-telegram-bot.
    :type app: Application
    :returns: None

    :example:
    >>> from telegram.ext import Application
    >>> from bot.handlers import register_handlers
    >>> app = Application.builder().token(TOKEN).build()
    >>> register_handlers(app)
    >>> app.run_polling()
    """
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(cb_actualize, pattern=f"^{CB_ACTUALIZE}$"))
    app.add_handler(CallbackQueryHandler(cb_get_filtered, pattern=f"^{CB_GET_FILTERED}$"))
    # Reply-кнопка Start — до общего хэндлера пароля
    app.add_handler(MessageHandler(filters.Regex(f"^{START_BUTTON_TEXT}$"), handle_start_button))
    # Хэндлер пароля — последним, ловит все текстовые сообщения не-команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    logger.info("Хэндлеры зарегистрированы.")
