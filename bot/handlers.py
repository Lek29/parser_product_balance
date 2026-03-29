"""
Хэндлеры Telegram-бота.

Обрабатывает команды и нажатия inline-кнопок:
- /start — приветствие; если не авторизован — запрашивает пароль
- handle_password — проверяет введённый пароль и авторизует пользователя
- CB_ACTUALIZE — проверка почты и обновление остатков (только для авторизованных)
- CB_GET_FILTERED — генерация и отправка отфильтрованного Excel-файла (только для авторизованных)
- CB_PARTIAL_START / reply-группы / callback порогов — частичная фильтрация по группам

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
    CB_PARTIAL_START,
    PARTIAL_REPLY_APPLY_TEXT,
    PARTIAL_REPLY_CANCEL_TEXT,
    START_BUTTON_TEXT,
    groups_reply_keyboard,
    main_keyboard,
    parse_partial_threshold_callback,
    partial_threshold_inline_keyboard,
    start_keyboard,
)
from bot.partial_filter_state import (
    FilterMode,
    PartialWizardStep,
    clear_pending_group_index,
    get_filter_mode,
    get_groups_order,
    get_partial_thresholds,
    get_pending_group_index,
    get_wizard_step,
    group_index_by_reply_label,
    group_name_by_index,
    init_partial_session,
    reset_session_to_global,
    set_partial_threshold,
    set_pending_group_index,
    set_wizard_step,
)
from storage.data_store import authorize, get_status_text, is_authorized

logger = logging.getLogger(__name__)

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
        f"≤ `{config.STOCK_THRESHOLD}` шт.\n"
        "🔀 *Частичная фильтрация* — разные пороги по группам товаров.\n\n"
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
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        await query.edit_message_text(
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
        await query.edit_message_text(
            "❌ Не удалось прочитать таблицу остатков.\n\nПопробуйте позже.",
            reply_markup=main_keyboard(),
        )
        return

    if not groups:
        await query.edit_message_text(
            "📭 В файле нет товарных групп для фильтра.\n\n"
            "Актуализируйте остатки.",
            reply_markup=main_keyboard(),
        )
        return

    init_partial_session(context, groups)
    await query.edit_message_text(
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
    2. Callback: частичная фильтрация (``pft:``, ``partial_start``), затем
       актуализация и общий Excel.
    3. Reply «▶️ Start» — до частичного и пароля.
    4. Частичная фильтрация (текст) — до пароля; останавливает цепочку через
       ``ApplicationHandlerStop``.
    5. MessageHandler для пароля — последним.

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
    app.add_handler(CallbackQueryHandler(cb_partial_threshold, pattern=r"^pft:"))
    app.add_handler(
        CallbackQueryHandler(cb_partial_start, pattern=f"^{CB_PARTIAL_START}$"),
    )
    app.add_handler(CallbackQueryHandler(cb_actualize, pattern=f"^{CB_ACTUALIZE}$"))
    app.add_handler(CallbackQueryHandler(cb_get_filtered, pattern=f"^{CB_GET_FILTERED}$"))
    # Reply-кнопка Start — до частичного сценария и пароля
    app.add_handler(MessageHandler(filters.Regex(f"^{START_BUTTON_TEXT}$"), handle_start_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_partial_messages))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    logger.info("Хэндлеры зарегистрированы.")
