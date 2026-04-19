"""
Хэндлеры Telegram-бота.

Обрабатывает команды и нажатия inline-кнопок:
- /start — приветствие; если не авторизован — запрашивает пароль
- handle_password — проверяет введённый пароль и авторизует пользователя
- CB_ACTUALIZE — проверка почты и обновление остатков (только для авторизованных)
- CB_GET_FILTERED — генерация и отправка отфильтрованного Excel-файла (только для авторизованных)
- CB_PARTIAL_START / reply-группы / callback порогов — частичная фильтрация по группам
- CB_GROUP_LIMIT_START / reply-группы / callback ``glt:`` — лимиты по группам (алерты)

Все хэндлеры асинхронные (python-telegram-bot v20+).
Ошибки перехватываются и сообщаются пользователю без падения бота.
Неавторизованные пользователи не получают доступ к функциям бота.

Защита от брутфорса:
- После MAX_AUTH_ATTEMPTS неверных попыток chat_id блокируется на AUTH_LOCKOUT_SECONDS.
- Счётчики хранятся в памяти (сбрасываются при перезапуске бота).
"""

import hmac
import logging
import os
import time

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from bot.keyboards import (
    CB_ACTUALIZE,
    CB_GET_FILTERED,
    CB_GROUP_LIMIT_START,
    CB_PARTIAL_START,
    LIMIT_REPLY_MENU_TEXT,
    PARTIAL_REPLY_APPLY_TEXT,
    PARTIAL_REPLY_CANCEL_TEXT,
    START_BUTTON_TEXT,
    group_limit_threshold_inline_keyboard,
    groups_reply_keyboard,
    main_keyboard,
    parse_group_limit_callback,
    parse_partial_threshold_callback,
    partial_threshold_inline_keyboard,
    start_keyboard,
)
from bot.partial_filter_state import (
    FilterMode,
    GroupLimitWizardStep,
    PartialWizardStep,
    clear_gl_pending_group_index,
    clear_pending_group_index,
    get_filter_mode,
    get_gl_groups_order,
    get_gl_pending_group_index,
    get_gl_wizard_step,
    get_groups_order,
    get_partial_thresholds,
    get_pending_group_index,
    get_wizard_step,
    gl_group_index_by_reply_label,
    gl_group_name_by_index,
    group_index_by_reply_label,
    group_name_by_index,
    init_group_limit_session,
    init_partial_session,
    reset_session_to_global,
    set_gl_pending_group_index,
    set_gl_wizard_step,
    set_partial_threshold,
    set_pending_group_index,
    set_wizard_step,
)
from storage.data_store import (
    authorize,
    clear_limit,
    get_limit,
    get_status_text,
    is_authorized,
    set_limit,
)

logger = logging.getLogger(__name__)


async def _edit_callback_message_safe(query, text: str, **kwargs: object) -> None:
    """
    Вызывает ``edit_message_text`` для callback; не падает, если текст и разметка совпадают
    с текущими (ответ Telegram «Message is not modified»).

    :param query: Объект CallbackQuery.
    :type query: telegram.CallbackQuery
    :param text: Новый текст сообщения.
    :type text: str
    :param kwargs: Аргументы для ``edit_message_text`` (parse_mode, reply_markup и т.д.).
    :type kwargs: object
    :returns: None
    """
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        msg = (getattr(e, "message", None) or str(e)).lower()
        if "message is not modified" in msg:
            logger.debug("edit_message_text: содержимое не изменилось, пропуск.")
            return
        raise

# CommandHandler по умолчанию использует только UpdateType.MESSAGES (message / edited_message).
# В Telegram Business команды приходят в business_message — без этого OR хэндлер не вызывается.
_COMMAND_FILTERS = (
    filters.UpdateType.MESSAGES | filters.UpdateType.BUSINESS_MESSAGES
)


def _text_message(update: Update) -> Message | None:
    """
    Возвращает сообщение с текстом для ответа: то же поле, что использует PTB
    в ``CommandHandler`` / ``MessageHandler``.

    Для чатов Telegram Business команда может приходить в ``business_message``,
    тогда ``update.message`` — ``None``, а ``effective_message`` указывает на текст.

    :param update: Входящий ``Update``.
    :type update: Update
    :returns: Объект сообщения или ``None``.
    :rtype: Message | None
    """
    return update.effective_message


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
    msg = _text_message(update)
    if not msg:
        logger.warning("cmd_start: нет сообщения (update_id=%s).", update.update_id)
        return

    chat_id = update.effective_chat.id

    if not _check_auth(chat_id):
        await msg.reply_text(
            _ASK_PASSWORD_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=start_keyboard(),
        )
        return

    reset_session_to_global(context)

    status = get_status_text()
    text = (
        "👋 *Парсер остатков*\n\n"
        f"📊 {status}\n\n"
        f"🎚 Порог отображения: `<= {config.STOCK_THRESHOLD}` шт.\n\n"
        "Уведомления по лимитам групп и еженедельная ревизия — в `/help`.\n\n"
        "Выберите действие:"
    )
    await msg.reply_text(
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
    msg = _text_message(update)
    if not msg:
        return

    chat_id = update.effective_chat.id

    if _check_auth(chat_id):
        return

    # Проверка блокировки
    if _is_locked_out(chat_id):
        until = _lockout_until.get(chat_id, 0)
        remaining = max(0, int(until - time.monotonic()))
        await msg.reply_text(
            f"🚫 Слишком много неверных попыток.\n\n"
            f"Повторите через {remaining} сек."
        )
        return

    entered = (msg.text or "").strip()

    # hmac.compare_digest требует одинаковый тип (str); работает за постоянное время
    password_correct = hmac.compare_digest(entered, config.BOT_PASSWORD)

    if password_correct:
        _reset_failed_attempts(chat_id)
        authorize(chat_id)
        reset_session_to_global(context)
        await msg.reply_text(
            "✅ *Доступ открыт!*\n\nНажмите кнопку ниже для запуска.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=start_keyboard(),
        )
        logger.info("Пользователь %d успешно авторизован через пароль.", chat_id)
    else:
        count = _record_failed_attempt(chat_id)
        remaining_attempts = max(0, MAX_AUTH_ATTEMPTS - count)
        if remaining_attempts > 0:
            await msg.reply_text(
                f"❌ Неверный пароль. "
                f"Осталось попыток: {remaining_attempts}."
            )
        else:
            await msg.reply_text(
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
    список команд, описание кнопок и блок про пороги по группам
    (уведомления, пресеты, ревизия; см. ``docs/fourty_role.md``, этап 9).

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
        f"≤ `{config.STOCK_THRESHOLD}` шт.\n"
        "🔀 *Частичная фильтрация* — разные пороги по группам товаров.\n"
        "🎚 *Лимиты по группам* — пороги для *уведомлений* о малых остатках "
        "по группе (см. блок ниже).\n\n"
        "*Пороги по группам — уведомления*\n"
        "Для товарной группы задаётся лимит (целое число шт.). Если по "
        "позиции в этой группе остаток *не выше* лимита, бот формирует "
        "сообщение со списком таких SKU.\n"
        "• Пресеты: `10`, `5`, `3` шт. или *свой порог* (ввод числом).\n"
        "• *Снять лимит* — в том же режиме настройки, для выбранной группы.\n"
        "• Сообщения уходят в *служебный чат Telegram*, который задаёт "
        "администратор в настройках сервера (не в личку к вам).\n"
        "• Пока лимит включён, группа на *надзоре*: дополнительно раз в "
        "неделю выполняется ревизия остатков по этим лимитам (и проверки "
        "после обновления данных). После снятия лимита группа с надзора "
        "снимается.\n\n"
        "🔒 Для работы с ботом требуется авторизация по паролю."
    )
    msg = _text_message(update)
    if not msg:
        logger.warning("cmd_help: нет сообщения (update_id=%s).", update.update_id)
        return
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
    msg = _text_message(update)
    if not msg:
        logger.warning("handle_start_button: нет сообщения (update_id=%s).", update.update_id)
        return

    chat_id = update.effective_chat.id

    if not _check_auth(chat_id):
        await msg.reply_text(
            _ASK_PASSWORD_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=start_keyboard(),
        )
        return

    reset_session_to_global(context)

    status = get_status_text()
    text = (
        "👋 *Парсер остатков*\n\n"
        f"📊 {status}\n\n"
        f"🎚 Порог отображения: `<= {config.STOCK_THRESHOLD}` шт.\n\n"
        "Уведомления по лимитам групп и еженедельная ревизия — в `/help`.\n\n"
        "Выберите действие:"
    )
    await msg.reply_text(
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
        from parser.mail_fetcher import MailFetchOutcome, fetch_and_save

        outcome = fetch_and_save()

        # SKIPPED_TODAY — IMAP не вызывался, сегодня уже проверялось
        if outcome == MailFetchOutcome.SKIPPED_TODAY:
            status = get_status_text()
            from storage.data_store import get_last_check_date

            check_date = get_last_check_date() or "сегодня"
            await query.edit_message_text(
                f"✅ Сегодня ({check_date}) уже проверялось.\n\n📊 {status}",
                reply_markup=main_keyboard(),
            )
            return

        # NO_CHANGES — IMAP проверен, нового письма нет
        if outcome == MailFetchOutcome.NO_CHANGES:
            status = get_status_text()
            await query.edit_message_text(
                f"✅ Проверено. Новых данных нет.\n\n📊 {status}",
                reply_markup=main_keyboard(),
            )
            return

        # Сбой архива — отдельно от «нет новых данных» (QA-001)
        if outcome == MailFetchOutcome.ARCHIVE_FAILURE:
            await query.edit_message_text(
                "❌ Не удалось скачать или распаковать архив с письма.\n\n"
                "Попробуйте позже. Если ошибка повторяется — "
                "проверьте логи сервера.",
                reply_markup=main_keyboard(),
            )
            return

        from services.stock_pipeline import run_parse_and_save_filtered

        await query.edit_message_text("⏳ Обрабатываю таблицу...")

        all_count, filtered_count = run_parse_and_save_filtered()

        reset_session_to_global(context)

        status = get_status_text()
        await query.edit_message_text(
            f"✅ *Остатки обновлены!*\n\n"
            f"📊 {status}\n"
            f"📦 Всего позиций: `{all_count}`\n"
            f"⚠️ Малый остаток (≤ {config.STOCK_THRESHOLD} шт.): "
            f"`{filtered_count}`",
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

        reset_session_to_global(context)

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


async def cb_partial_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Вход в сценарий частичной фильтрации с главного inline-меню.

    Проверяет наличие ``current.xlsx``, строит список групп и показывает
    reply-клавиатуру с инструкцией.

    :param update: Callback с кнопки «Частичная фильтрация».
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: При успехе — :func:`init_partial_session` (режим PARTIAL,
                   ``user_data``), исходящие сообщения в чат; иначе правка
                   сообщения с inline-меню без смены режима.
    """
    query = update.callback_query
    await query.answer()

    if not _check_auth(query.message.chat_id):
        await _edit_callback_message_safe(query, _NOT_AUTH_TEXT)
        return

    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        await _edit_callback_message_safe(
            query,
            "📭 Нет файла остатков.\n\n"
            "Сначала нажмите «Актуализировать остатки».",
            reply_markup=main_keyboard(),
        )
        return

    try:
        from parser.excel_parser import list_product_groups

        groups = list_product_groups()
    except Exception as e:
        logger.error("Частичная фильтрация: ошибка списка групп: %s", e, exc_info=True)
        await _edit_callback_message_safe(
            query,
            "❌ Не удалось прочитать таблицу остатков.\n\nПопробуйте позже.",
            reply_markup=main_keyboard(),
        )
        return

    if not groups:
        await _edit_callback_message_safe(
            query,
            "📭 В файле нет товарных групп для фильтра.\n\n"
            "Актуализируйте остатки.",
            reply_markup=main_keyboard(),
        )
        return

    init_partial_session(context, groups)
    await _edit_callback_message_safe(
        query,
        "🔀 *Частичная фильтрация*\n\n"
        "Ниже появятся кнопки групп. Выберите группу — затем порог "
        "в сообщении с кнопками. Можно настроить несколько групп и "
        "нажать *«Применить»*.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Выберите группу на клавиатуре:",
        reply_markup=groups_reply_keyboard(groups),
    )


async def cb_group_limit_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Вход в мастер лимитов по группам с главного inline-меню.

    Лимиты сохраняются в ``storage`` и используются при проверке остатков
    после парсинга (см. ``docs/fourty_role.md``).

    :param update: Callback с кнопки «Лимиты по группам».
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: :func:`init_group_limit_session`, исходящие сообщения.
    """
    query = update.callback_query
    await query.answer()

    if not _check_auth(query.message.chat_id):
        await _edit_callback_message_safe(query, _NOT_AUTH_TEXT)
        return

    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        await _edit_callback_message_safe(
            query,
            "📭 Нет файла остатков.\n\n"
            "Сначала нажмите «Актуализировать остатки».",
            reply_markup=main_keyboard(),
        )
        return

    try:
        from parser.excel_parser import list_product_groups

        groups = list_product_groups()
    except Exception as e:
        logger.error(
            "Лимиты по группам: ошибка списка групп: %s",
            e,
            exc_info=True,
        )
        await _edit_callback_message_safe(
            query,
            "❌ Не удалось прочитать таблицу остатков.\n\nПопробуйте позже.",
            reply_markup=main_keyboard(),
        )
        return

    if not groups:
        await _edit_callback_message_safe(
            query,
            "📭 В файле нет товарных групп.\n\nАктуализируйте остатки.",
            reply_markup=main_keyboard(),
        )
        return

    init_group_limit_session(context, groups)
    await _edit_callback_message_safe(
        query,
        "🎚 *Лимиты по группам*\n\n"
        "Ниже — кнопки групп. Выберите группу, затем лимит: `10`, `5`, "
        "`3` или *свой* (целое ≥ 0). Кнопка *«Снять лимит»* удаляет порог "
        "для выбранной группы.\n\n"
        "Нажмите *«В меню»*, чтобы выйти в главное меню.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Выберите группу на клавиатуре:",
        reply_markup=groups_reply_keyboard(
            groups,
            footer_done_text=LIMIT_REPLY_MENU_TEXT,
        ),
    )


async def cb_group_limit_threshold(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Выбор лимита (пресет, свой, снять) по inline-кнопке ``glt:``.

    :param update: Callback с клавиатуры лимитов.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: Запись в ``storage`` через :func:`storage.data_store.set_limit`
                  / :func:`storage.data_store.clear_limit`, шаги мастера в
                  ``user_data``.
    """
    query = update.callback_query
    await query.answer()

    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    if get_filter_mode(context) != FilterMode.GROUP_LIMIT_SETUP:
        await query.edit_message_text("Сессия устарела. Отправьте /start.")
        return

    parsed = parse_group_limit_callback(query.data or "")
    if not parsed:
        return

    idx, val = parsed
    gname = gl_group_name_by_index(context, idx)
    if gname is None:
        await query.edit_message_text("Группа не найдена. Отправьте /start.")
        return

    if val == "clear":
        clear_limit(gname)
        clear_gl_pending_group_index(context)
        set_gl_wizard_step(context, GroupLimitWizardStep.GROUPS_LIST)
        await query.edit_message_text(
            f"🗑 Лимит для группы *{gname}* снят.\n\n"
            "Выберите другую группу на клавиатуре.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if val == "custom":
        set_gl_pending_group_index(context, idx)
        set_gl_wizard_step(context, GroupLimitWizardStep.AWAITING_LIMIT_TEXT)
        await query.edit_message_text(
            f"✏️ Группа: *{gname}*\n\n"
            "Введите целое число ≥ 0 — лимит остатка (включительно).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if isinstance(val, int):
        set_limit(gname, val)
        clear_gl_pending_group_index(context)
        set_gl_wizard_step(context, GroupLimitWizardStep.GROUPS_LIST)
        await query.edit_message_text(
            f"✅ Для группы *{gname}* установлен лимит `{val}` шт.\n\n"
            "Выберите следующую группу на клавиатуре.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cb_partial_threshold(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Выбор порога (пресет или «свой порог») по inline-кнопке ``pft:``.

    :param update: Callback с клавиатуры порогов.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: Запись в черновик (:func:`set_partial_threshold`) и/или
                   ``set_pending_group_index``, ``set_wizard_step``;
                   редактирование сообщения с inline-клавиатурой.
    """
    query = update.callback_query
    await query.answer()

    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    if get_filter_mode(context) != FilterMode.PARTIAL_FILTER:
        await query.edit_message_text(
            "Сессия устарела. Отправьте /start.",
        )
        return

    parsed = parse_partial_threshold_callback(query.data or "")
    if not parsed:
        return

    idx, val = parsed
    gname = group_name_by_index(context, idx)
    if gname is None:
        await query.edit_message_text(
            "Группа не найдена. Отправьте /start.",
        )
        return

    if val == "custom":
        set_pending_group_index(context, idx)
        set_wizard_step(context, PartialWizardStep.AWAITING_THRESHOLD_TEXT)
        await query.edit_message_text(
            f"✏️ Группа: *{gname}*\n\n"
            "Введите целое число — порог остатка (неотрицательное).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not isinstance(val, int):
        return
    set_partial_threshold(context, gname, val)
    clear_pending_group_index(context)
    set_wizard_step(context, PartialWizardStep.GROUPS_LIST)
    await query.edit_message_text(
        f"✅ *{gname}*: порог ≤ `{val}` шт.\n\n"
        "Выберите следующую группу на клавиатуре или «Применить».",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _partial_apply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Формирует xlsx по черновику порогов и отправляет пользователю.

    **Политика граничных случаев** (``docs/partial_filtering.md``, шаг 5):

    * Черновик без ни одного порога — выгрузка не выполняется, глобальный
      ``STOCK_THRESHOLD`` не подмешивается; пользователю показывается подсказка.
    * Отбор дал 0 позиций — файл не отправляется, только сообщение в чат.

    :param update: Сообщение с reply «Применить».
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: Чтение/запись ``storage`` (xlsx), отправка документа,
                   :func:`reset_session_to_global` при успехе или
                   ``FileNotFoundError``, записи в лог.
    """
    msg = update.effective_message
    chat_id = update.effective_chat.id
    thresholds = get_partial_thresholds(context)

    if not thresholds:
        logger.info(
            "Частичная выгрузка: отклонено — пустой черновик порогов (chat_id=%s).",
            chat_id,
        )
        await msg.reply_text(
            "⚠️ Не задан ни один порог по группам.\n\n"
            "Нажмите на название группы на клавиатуре и выберите порог. "
            f"Глобальный порог ({config.STOCK_THRESHOLD} шт.) для частичного "
            "режима не подставляется.",
            reply_markup=groups_reply_keyboard(get_groups_order(context)),
        )
        return

    await msg.reply_text("⏳ Формирую файл...")

    try:
        from parser.excel_parser import (
            filter_by_group_thresholds,
            generate_filtered_partial_xlsx,
            parse_stock,
        )

        all_items = parse_stock()
        filtered = filter_by_group_thresholds(all_items, thresholds)

        if not filtered:
            logger.warning(
                "Частичная выгрузка: 0 позиций после фильтра "
                "(правил_групп=%d, chat_id=%s). Файл не отправляется.",
                len(thresholds),
                chat_id,
            )
            await msg.reply_text(
                "📭 Нет позиций под критерии.\n\n"
                "Попробуйте увеличить пороги по группам или проверьте остатки "
                "в исходной таблице.",
                reply_markup=groups_reply_keyboard(get_groups_order(context)),
            )
            return

        path = generate_filtered_partial_xlsx(filtered)
        status = get_status_text()
        caption = (
            f"📊 {status}\n"
            f"Частичный фильтр: *{len(thresholds)}* групп, "
            f"*{len(filtered)}* поз."
        )

        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename="остатки_частичный_фильтр.xlsx",
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                write_timeout=120,
                read_timeout=60,
            )

        logger.info(
            "Частичная выгрузка OK: файл=%s, правил_групп=%d, позиций=%d, "
            "chat_id=%s",
            os.path.basename(path),
            len(thresholds),
            len(filtered),
            chat_id,
        )

        reset_session_to_global(context)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📤 Файл отправлен выше.",
            reply_markup=start_keyboard(),
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Выберите действие:",
            reply_markup=main_keyboard(),
        )

    except FileNotFoundError:
        await msg.reply_text(
            "❌ Файл остатков не найден.\n\n"
            "Сначала «Актуализировать остатки».",
            reply_markup=main_keyboard(),
        )
        reset_session_to_global(context)
    except Exception as e:
        logger.error("Частичная выгрузка: %s", e, exc_info=True)
        await msg.reply_text(
            "❌ Ошибка при формировании файла. Попробуйте позже.",
            reply_markup=groups_reply_keyboard(get_groups_order(context)),
        )


async def _partial_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Выход из частичной фильтрации без выгрузки файла.

    :param update: Сообщение с reply «Отмена».
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: :func:`reset_session_to_global`; исходящие сообщения
                   с ``start_keyboard`` и ``main_keyboard``.
    """
    chat_id = update.effective_chat.id
    reset_session_to_global(context)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Частичная фильтрация отменена.",
        reply_markup=start_keyboard(),
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Выберите действие:",
        reply_markup=main_keyboard(),
    )


async def handle_group_limit_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Текстовые сообщения в режиме лимитов по группам: группа, свой лимит, выход.

    Для остальных режимов сразу выходит — дальше обрабатывают другие хэндлеры.

    :param update: Текстовое сообщение.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :raises ApplicationHandlerStop: Если сообщение обработано здесь.
    """
    msg = _text_message(update)
    if not msg or not msg.text:
        return

    if not _check_auth(update.effective_chat.id):
        return

    if get_filter_mode(context) != FilterMode.GROUP_LIMIT_SETUP:
        return

    if msg.text.strip() == START_BUTTON_TEXT:
        return

    text = msg.text.strip()
    step = get_gl_wizard_step(context)

    if step == GroupLimitWizardStep.AWAITING_LIMIT_TEXT:
        idx = get_gl_pending_group_index(context)
        gname = (
            gl_group_name_by_index(context, idx) if idx is not None else None
        )
        if gname is None:
            set_gl_wizard_step(context, GroupLimitWizardStep.GROUPS_LIST)
            clear_gl_pending_group_index(context)
            await msg.reply_text(
                "Сессия ввода сброшена. Выберите группу на клавиатуре.",
                reply_markup=groups_reply_keyboard(
                    get_gl_groups_order(context),
                    footer_done_text=LIMIT_REPLY_MENU_TEXT,
                ),
            )
            raise ApplicationHandlerStop

        try:
            lim = int(text)
            if lim < 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("Введите целое число ≥ 0.")
            raise ApplicationHandlerStop

        set_limit(gname, lim)
        clear_gl_pending_group_index(context)
        set_gl_wizard_step(context, GroupLimitWizardStep.GROUPS_LIST)
        await msg.reply_text(
            f"✅ Для группы *{gname}* установлен лимит `{lim}` шт.\n"
            "Выберите другую группу или нажмите «В меню».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=groups_reply_keyboard(
                get_gl_groups_order(context),
                footer_done_text=LIMIT_REPLY_MENU_TEXT,
            ),
        )
        raise ApplicationHandlerStop

    if text == LIMIT_REPLY_MENU_TEXT:
        reset_session_to_global(context)
        await msg.reply_text(
            "Настройка лимитов завершена.",
            reply_markup=start_keyboard(),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Выберите действие:",
            reply_markup=main_keyboard(),
        )
        raise ApplicationHandlerStop

    if text == PARTIAL_REPLY_CANCEL_TEXT:
        reset_session_to_global(context)
        await msg.reply_text(
            "Настройка лимитов отменена.",
            reply_markup=start_keyboard(),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Выберите действие:",
            reply_markup=main_keyboard(),
        )
        raise ApplicationHandlerStop

    idx = gl_group_index_by_reply_label(context, text)
    if idx is None:
        await msg.reply_text(
            "Не понял. Выберите группу с клавиатуры "
            "или «В меню» / «Отмена».",
        )
        raise ApplicationHandlerStop

    gname = gl_group_name_by_index(context, idx)
    if gname is None:
        await msg.reply_text("Группа не найдена. Отправьте /start.")
        raise ApplicationHandlerStop

    set_gl_wizard_step(context, GroupLimitWizardStep.LIMIT_FOR_GROUP)
    current = get_limit(gname)
    if current is not None:
        cur_line = f"Текущий сохранённый лимит: `{current}` шт."
    else:
        cur_line = "Лимит пока *не задан*."

    await msg.reply_text(
        f"Группа: *{gname}*\n{cur_line}\n\nВыберите лимит:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=group_limit_threshold_inline_keyboard(idx),
    )
    raise ApplicationHandlerStop


async def handle_partial_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Текстовые сообщения в режиме частичной фильтрации: группы, порог, Применить.

    Для остальных режимов сразу выходит — дальше обрабатывает ``handle_password``.

    :param update: Текстовое сообщение.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :raises ApplicationHandlerStop: Если сообщение обработано здесь
                                      (остановка цепочки хэндлеров).
    :side effects: При обработке — изменение ``user_data``, ответы в чат,
                   вызов :func:`_partial_apply` / :func:`_partial_cancel`.
    """
    msg = _text_message(update)
    if not msg or not msg.text:
        return

    if not _check_auth(update.effective_chat.id):
        return

    if get_filter_mode(context) != FilterMode.PARTIAL_FILTER:
        return

    text = msg.text.strip()
    if text == START_BUTTON_TEXT:
        return
    step = get_wizard_step(context)

    if step == PartialWizardStep.AWAITING_THRESHOLD_TEXT:
        idx = get_pending_group_index(context)
        gname = group_name_by_index(context, idx) if idx is not None else None
        if gname is None:
            set_wizard_step(context, PartialWizardStep.GROUPS_LIST)
            clear_pending_group_index(context)
            await msg.reply_text(
                "Сессия ввода сброшена. Выберите группу на клавиатуре.",
            )
            raise ApplicationHandlerStop

        try:
            thr = int(text)
            if thr < 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("Введите целое неотрицательное число.")
            raise ApplicationHandlerStop

        set_partial_threshold(context, gname, thr)
        clear_pending_group_index(context)
        set_wizard_step(context, PartialWizardStep.GROUPS_LIST)
        await msg.reply_text(
            f"✅ *{gname}*: порог ≤ `{thr}` шт.\n"
            "Выберите следующую группу или «Применить».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=groups_reply_keyboard(get_groups_order(context)),
        )
        raise ApplicationHandlerStop

    if text == PARTIAL_REPLY_APPLY_TEXT:
        await _partial_apply(update, context)
        raise ApplicationHandlerStop

    if text == PARTIAL_REPLY_CANCEL_TEXT:
        await _partial_cancel(update, context)
        raise ApplicationHandlerStop

    idx = group_index_by_reply_label(context, text)
    if idx is None:
        await msg.reply_text(
            "Не понял. Выберите группу с клавиатуры "
            "или «Применить» / «Отмена».",
        )
        raise ApplicationHandlerStop

    gname = group_name_by_index(context, idx)
    if gname is None:
        await msg.reply_text("Группа не найдена. Отправьте /start.")
        raise ApplicationHandlerStop

    set_wizard_step(context, PartialWizardStep.THRESHOLD_FOR_GROUP)
    await msg.reply_text(
        f"Группа: *{gname}*\nВыберите порог:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=partial_threshold_inline_keyboard(idx),
    )
    raise ApplicationHandlerStop


def register_handlers(app: Application) -> None:
    """
    Регистрирует все хэндлеры команд, callback-кнопок и текстовых сообщений.

    Порядок регистрации важен:
    1. Команды (/start, /help) — первыми, чтобы telegram не путал с текстом.
    2. Callback: ``glt:`` и ``group_limit_start``, частичная фильтрация
       (``pft:``, ``partial_start``), затем актуализация и общий Excel.
    3. Reply «▶️ Start» — до мастеров и пароля.
    4. Лимиты по группам (текст) — до частичного и пароля;
       ``ApplicationHandlerStop``.
    5. Частичная фильтрация (текст) — до пароля;
       ``ApplicationHandlerStop``.
    6. MessageHandler для пароля — последним.

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
    app.add_handler(CommandHandler("start", cmd_start, filters=_COMMAND_FILTERS))
    app.add_handler(
        CommandHandler("strat", cmd_start, filters=_COMMAND_FILTERS),
    )  # опечатка /strat → как /start
    app.add_handler(CommandHandler("help", cmd_help, filters=_COMMAND_FILTERS))
    app.add_handler(CallbackQueryHandler(cb_group_limit_threshold, pattern=r"^glt:"))
    app.add_handler(
        CallbackQueryHandler(
            cb_group_limit_start,
            pattern=f"^{CB_GROUP_LIMIT_START}$",
        ),
    )
    app.add_handler(CallbackQueryHandler(cb_partial_threshold, pattern=r"^pft:"))
    app.add_handler(
        CallbackQueryHandler(cb_partial_start, pattern=f"^{CB_PARTIAL_START}$"),
    )
    app.add_handler(CallbackQueryHandler(cb_actualize, pattern=f"^{CB_ACTUALIZE}$"))
    app.add_handler(CallbackQueryHandler(cb_get_filtered, pattern=f"^{CB_GET_FILTERED}$"))
    # Reply-кнопка Start — до частичного сценария и пароля
    app.add_handler(MessageHandler(filters.Regex(f"^{START_BUTTON_TEXT}$"), handle_start_button))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_group_limit_messages,
        ),
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_partial_messages))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    logger.info("Хэндлеры зарегистрированы.")
