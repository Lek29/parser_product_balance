"""
Хэндлеры Telegram-бота.

Обрабатывает команды и нажатия inline-кнопок:
- /start — приветствие; если не авторизован — запрашивает пароль
- handle_password — проверяет введённый пароль и авторизует пользователя
- CB_ACTUALIZE — проверка почты и обновление остатков (только для авторизованных)
- CB_GET_FILTERED — генерация и отправка отфильтрованного Excel-файла (только для авторизованных)
- CB_PARTIAL_START / reply-группы / callback порогов — частичная фильтрация по группам
- CB_GROUP_LIMIT_START / reply-группы / callback ``glt:`` — лимиты по группам (алерты)
- CB_RED_BIN_START / кнопки группа → бренд → товар / callback ``rb:`` — точечный Red Bin

Все хэндлеры асинхронные (python-telegram-bot v20+).
Ошибки перехватываются и сообщаются пользователю без падения бота.
Неавторизованные пользователи не получают доступ к функциям бота.

Защита от брутфорса:
- После MAX_AUTH_ATTEMPTS неверных попыток chat_id блокируется на AUTH_LOCKOUT_SECONDS.
- Счётчики хранятся в памяти (сбрасываются при перезапуске бота).
"""

import asyncio
import hmac
import logging
import os
import time
from html import escape as html_escape

from telegram import Message, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
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
    CB_RED_BIN_START,
    LIMIT_REPLY_MENU_TEXT,
    PARTIAL_REPLY_APPLY_TEXT,
    PARTIAL_REPLY_CANCEL_TEXT,
    RED_BIN_LIST_TEXT,
    RED_BIN_PAGE_SIZE,
    START_BUTTON_TEXT,
    group_limit_threshold_inline_keyboard,
    groups_reply_keyboard,
    main_keyboard,
    normalize_reply_button_text,
    parse_group_limit_callback,
    parse_partial_threshold_callback,
    parse_red_bin_callback,
    partial_threshold_inline_keyboard,
    red_bin_browse_keyboard,
    red_bin_export_keyboard,
    red_bin_limit_keyboard,
    red_bin_reply_keyboard,
    start_keyboard,
)
from bot.partial_filter_state import (
    FilterMode,
    GroupLimitWizardStep,
    PartialWizardStep,
    RedBinStep,
    clear_gl_pending_group_index,
    clear_pending_group_index,
    clear_red_bin_pending_index,
    get_filter_mode,
    get_gl_groups_order,
    get_gl_pending_group_index,
    get_gl_wizard_step,
    get_groups_order,
    get_partial_thresholds,
    get_pending_group_index,
    get_red_bin_brand,
    get_red_bin_group,
    get_red_bin_line,
    get_red_bin_matches,
    get_red_bin_pending_index,
    get_red_bin_step,
    get_red_bin_tree,
    get_wizard_step,
    gl_group_index_by_reply_label,
    gl_group_name_by_index,
    group_index_by_reply_label,
    group_name_by_index,
    init_group_limit_session,
    init_partial_session,
    init_red_bin_session,
    is_known_product_group_reply_text,
    reset_session_to_global,
    set_gl_pending_group_index,
    set_gl_wizard_step,
    set_partial_threshold,
    set_pending_group_index,
    set_red_bin_brand,
    set_red_bin_group,
    set_red_bin_line,
    set_red_bin_matches,
    set_red_bin_pending_index,
    set_red_bin_step,
    set_red_bin_tree,
    set_wizard_step,
)
from storage.data_store import (
    authorize,
    clear_limit,
    clear_red_bin,
    get_limit,
    get_status_text,
    is_authorized,
    list_red_bin,
    set_limit,
    upsert_red_bin,
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


async def _answer_callback_query_safe(
    query,
    *,
    max_attempts: int = 3,
) -> bool:
    """
    Отвечает на callback query с повтором при :class:`telegram.error.NetworkError`.

    Сбои TLS/прокси к ``api.telegram.org`` иначе рвут хэндлер на первой строке,
    пользователь видит «тишину» и крутящийся индикатор у кнопки.

    :param query: Объект ``CallbackQuery``.
    :param max_attempts: Число попыток с паузой между ними.
    :returns: ``True`` если ответ принят API; иначе ``False``.
    """
    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            await query.answer()
            return True
        except NetworkError as e:
            last_err = e
            logger.warning(
                "answer_callback_query: сеть (попытка %d/%d): %s",
                attempt + 1,
                max_attempts,
                e,
            )
            if attempt + 1 < max_attempts:
                await asyncio.sleep(0.8 * (attempt + 1))
    logger.error(
        "answer_callback_query: не удалось после %d попыток: %s",
        max_attempts,
        last_err,
        exc_info=last_err is not None,
    )
    return False


async def _notify_telegram_network_unreachable(
    context: ContextTypes.DEFAULT_TYPE,
    query,
) -> None:
    """
    Пытается отправить в чат пояснение при полном отказе ``answer_callback_query``.

    :param context: Контекст PTB.
    :param query: Тот же ``CallbackQuery`` (для ``chat_id``).
    :returns: None
    """
    msg = getattr(query, "message", None)
    chat_id = getattr(msg, "chat_id", None) if msg else None
    if chat_id is None:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ Нет связи с Telegram (сеть или прокси).\n\n"
                "Проверьте интернет. Если заданы HTTP_PROXY/HTTPS_PROXY — "
                "убедитесь, что через них доступен api.telegram.org, или "
                "временно отключите прокси для этого процесса."
            ),
        )
    except TelegramError as e:
        logger.warning("Не удалось отправить подсказку о сети: %s", e)


# CommandHandler по умолчанию использует только UpdateType.MESSAGES (message / edited_message).
# В Telegram Business команды приходят в business_message — без этого OR хэндлер не вызывается.
_COMMAND_FILTERS = filters.UpdateType.MESSAGES | filters.UpdateType.BUSINESS_MESSAGES


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
_ASK_PASSWORD_TEXT = "🔒 *Доступ закрыт*\n\nВведите пароль для входа:"

# Текст для неавторизованных при нажатии кнопок
_NOT_AUTH_TEXT = "🔒 Нет доступа.\n\nОтправьте /start и введите пароль."

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
            chat_id,
            AUTH_LOCKOUT_SECONDS,
            count,
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
        "Уведомления по лимитам групп, Red Bin и еженедельная ревизия — в `/help`."
    )
    await msg.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Выберите действие:",
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
        await msg.reply_text(f"🚫 Слишком много неверных попыток.\n\nПовторите через {remaining} сек.")
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
            await msg.reply_text(f"❌ Неверный пароль. Осталось попыток: {remaining_attempts}.")
        else:
            await msg.reply_text(f"🚫 Слишком много неверных попыток.\n\nПовторите через {AUTH_LOCKOUT_SECONDS} сек.")
        logger.warning(
            "Неверный пароль от пользователя %d (попытка %d/%d).",
            chat_id,
            count,
            MAX_AUTH_ATTEMPTS,
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
        "по группе (см. блок ниже).\n"
        "🔴 *Red Bin* — лимит на *одну* позицию: сообщения, когда остаток "
        "дошёл до лимита, когда его начали продавать и когда стало 0.\n\n"
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
        "*Red Bin*\n"
        "Кнопками: группа → бренд → модель (мячи → Mikasa → мяч Mikasa MV300), "
        "затем лимит. Длинный список листается стрелками, «Назад» — на уровень выше. "
        "Пресеты `10` / `5` / `3` или своё число ≥ 1. "
        "*Снять* — на карточке товара. *Список* — все лимиты в чат или файлом.\n"
        "На следующем полном обновлении остатков бот пишет в служебный чат:\n"
        "• остаток стал равен лимиту;\n"
        "• остаток стал меньше лимита (один раз, пока не пополнят);\n"
        "• остаток 0 или строка пропала из файла.\n"
        "Пока остаток ползёт внутри корзины (4→3→2), новых сообщений нет.\n\n"
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
        "Уведомления по лимитам групп, Red Bin и еженедельная ревизия — в `/help`."
    )
    await msg.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Выберите действие:",
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
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return

    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    await query.edit_message_text("⏳ Проверяю почту...")

    try:
        from parser.mail_fetcher import MailFetchOutcome, fetch_and_save

        # Кнопка всегда принудительно качает лучшее найденное письмо (force=True),
        # игнорируя кэш текущей недели и проверку "письмо не новее сохранённого".
        outcome = fetch_and_save(force=True)

        from services.stock_pipeline import run_parse_and_save_filtered

        # SKIPPED_TODAY с force=True не вернётся, но оставляем на всякий случай
        if outcome == MailFetchOutcome.SKIPPED_TODAY:
            await query.edit_message_text("⏳ Файл этой недели уже есть — сверяю кэш таблицы...")
            all_count, filtered_count = run_parse_and_save_filtered(notify_group_limits=False)
            reset_session_to_global(context)
            status = get_status_text()
            await query.edit_message_text(
                "✅ На этой неделе файл с почты уже скачивали.\n\n"
                f"📊 {status}\n"
                f"📦 Позиций: `{all_count}` · малый остаток: `{filtered_count}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_keyboard(),
            )
            return

        # NO_CHANGES с force=True означает только одно: писем с архивами в ящике вообще нет
        if outcome == MailFetchOutcome.NO_CHANGES:
            await query.edit_message_text("⏳ Подходящих архивов на почте не найдено — сверяю локальный кэш таблицы...")
            all_count, filtered_count = run_parse_and_save_filtered(notify_group_limits=False)
            reset_session_to_global(context)
            status = get_status_text()
            await query.edit_message_text(
                f"✅ Проверено. Новых данных на почте нет.\n\n"
                f"📊 {status}\n"
                f"📦 Позиций: `{all_count}` · малый остаток: `{filtered_count}`",
                parse_mode=ParseMode.MARKDOWN,
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

        await query.edit_message_text("⏳ Обрабатываю таблицу...")

        all_count, filtered_count = run_parse_and_save_filtered(notify_group_limits=False)

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
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return

    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return

    await query.edit_message_text("⏳ Готовлю файл...")

    try:
        from storage.data_store import filtered_xlsx_ready_for_send

        xlsx_path = config.FILTERED_XLSX_PATH
        filtered_count: int | None = None

        if filtered_xlsx_ready_for_send():
            # P1/3B: отдаём готовый filtered.xlsx без повторного parse_stock
            logger.info("Отправка готового filtered.xlsx без повторного парсинга.")
        else:
            from parser.excel_parser import generate_filtered_xlsx, get_filtered_items

            await query.edit_message_text("⏳ Формирую файл...")
            filtered = get_filtered_items()
            if not filtered:
                await query.edit_message_text(
                    f"📭 Товаров с остатком ≤ {config.STOCK_THRESHOLD} шт. не найдено.\n\n"
                    "Попробуйте сначала актуализировать остатки.",
                    reply_markup=main_keyboard(),
                )
                return
            xlsx_path = generate_filtered_xlsx(filtered)
            filtered_count = len(filtered)

        if filtered_count is None:
            # Счётчик из data.json при отдаче с диска (без тяжёлого parse)
            from parser.excel_parser import filter_by_threshold
            from storage.data_store import load_items

            filtered_count = len(filter_by_threshold(load_items()))

        if filtered_count == 0:
            await query.edit_message_text(
                f"📭 Товаров с остатком ≤ {config.STOCK_THRESHOLD} шт. не найдено.\n\n"
                "Попробуйте сначала актуализировать остатки.",
                reply_markup=main_keyboard(),
            )
            return

        status = get_status_text()
        caption = f"📊 {status}\n⚠️ Позиций с малым остатком: *{filtered_count}*"

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
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return

    if not _check_auth(query.message.chat_id):
        await _edit_callback_message_safe(query, _NOT_AUTH_TEXT)
        return

    if get_filter_mode(context) == FilterMode.PARTIAL_FILTER:
        await _edit_callback_message_safe(
            query,
            "🔀 *Частичная фильтрация* уже активна.\n\n"
            "Продолжайте на *reply-клавиатуре* внизу чата: выберите группу, "
            "затем порог в сообщении с *inline-кнопками*, либо нажмите "
            "*«Применить»*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        return

    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        await _edit_callback_message_safe(
            query,
            "📭 Нет файла остатков.\n\nСначала нажмите «Актуализировать остатки».",
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
            "📭 В файле нет товарных групп для фильтра.\n\nАктуализируйте остатки.",
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
        reply_markup=main_keyboard(),
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
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return

    if not _check_auth(query.message.chat_id):
        await _edit_callback_message_safe(query, _NOT_AUTH_TEXT)
        return

    if get_filter_mode(context) == FilterMode.GROUP_LIMIT_SETUP:
        await _edit_callback_message_safe(
            query,
            "🎚 *Лимиты по группам* уже открыты.\n\nПродолжайте на *reply-клавиатуре* внизу чата.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        return

    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        await _edit_callback_message_safe(
            query,
            "📭 Нет файла остатков.\n\nСначала нажмите «Актуализировать остатки».",
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
        reply_markup=main_keyboard(),
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
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return

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
            f"🗑 Лимит для группы <b>{html_escape(gname)}</b> снят.\n\nВыберите другую группу на клавиатуре.",
            parse_mode=ParseMode.HTML,
        )
        return

    if val == "custom":
        set_gl_pending_group_index(context, idx)
        set_gl_wizard_step(context, GroupLimitWizardStep.AWAITING_LIMIT_TEXT)
        await query.edit_message_text(
            f"✏️ Группа: <b>{html_escape(gname)}</b>\n\nВведите целое число ≥ 0 — лимит остатка (включительно).",
            parse_mode=ParseMode.HTML,
        )
        return

    if isinstance(val, int):
        set_limit(gname, val)
        clear_gl_pending_group_index(context)
        set_gl_wizard_step(context, GroupLimitWizardStep.GROUPS_LIST)
        await query.edit_message_text(
            f"✅ Для группы <b>{html_escape(gname)}</b> установлен лимит "
            f"<code>{val}</code> шт.\n\n"
            "Выберите следующую группу на клавиатуре.",
            parse_mode=ParseMode.HTML,
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
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return

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
            f"✏️ Группа: <b>{html_escape(gname)}</b>\n\nВведите целое число — порог остатка (неотрицательное).",
            parse_mode=ParseMode.HTML,
        )
        return

    if not isinstance(val, int):
        return
    set_partial_threshold(context, gname, val)
    clear_pending_group_index(context)
    set_wizard_step(context, PartialWizardStep.GROUPS_LIST)
    await query.edit_message_text(
        f"✅ <b>{html_escape(gname)}</b>: порог ≤ <code>{val}</code> шт.\n\n"
        "Выберите следующую группу на клавиатуре или «Применить».",
        parse_mode=ParseMode.HTML,
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
    draft = get_partial_thresholds(context)
    used_global_fallback = False
    if draft:
        rules: dict[str, int] = dict(draft)
    else:
        order = list(get_groups_order(context))
        if not order:
            try:
                from parser.excel_parser import list_product_groups

                order = list_product_groups()
            except Exception as e:
                logger.error(
                    "Частичная выгрузка: нет черновика и не удалось список групп: %s",
                    e,
                    exc_info=True,
                )
                order = []
        if not order:
            logger.info(
                "Частичная выгрузка: отклонено — нет групп (chat_id=%s).",
                chat_id,
            )
            await msg.reply_text(
                "⚠️ Нет списка групп для выгрузки.\n\n"
                "Откройте «🔀 Частичная фильтрация» заново после актуализации "
                "остатков.",
                reply_markup=main_keyboard(),
            )
            return
        rules = {g: int(config.STOCK_THRESHOLD) for g in order}
        used_global_fallback = True
        logger.info(
            "Частичная выгрузка: черновик пуст — применён общий порог ≤%s шт. ко всем %d группам (chat_id=%s).",
            config.STOCK_THRESHOLD,
            len(rules),
            chat_id,
        )

    await msg.reply_text("⏳ Формирую файл...")

    try:
        from parser.excel_parser import (
            filter_by_group_thresholds,
            generate_filtered_partial_xlsx,
            parse_stock,
        )

        all_items = parse_stock()
        filtered = filter_by_group_thresholds(all_items, rules)

        if not filtered:
            logger.warning(
                "Частичная выгрузка: 0 позиций после фильтра (правил_групп=%d, chat_id=%s). Файл не отправляется.",
                len(rules),
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
        fb_line = ""
        if used_global_fallback:
            fb_line = (
                f"\n<i>Пороги по группам не задавались — для всех {len(rules)} "
                f"групп использован общий ≤ {config.STOCK_THRESHOLD} шт.</i>\n"
            )
        caption = (
            f"📊 {html_escape(status)}{fb_line}Частичный фильтр: <b>{len(rules)}</b> групп, <b>{len(filtered)}</b> поз."
        )

        try:
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename="остатки_частичный_фильтр.xlsx",
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    write_timeout=120,
                    read_timeout=60,
                )
        except TelegramError as send_err:
            logger.error(
                "Частичная выгрузка: send_document: %s",
                send_err,
                exc_info=True,
            )
            await msg.reply_text(
                "❌ Не удалось отправить файл в Telegram. Попробуйте ещё раз или проверьте размер/сеть.",
                reply_markup=groups_reply_keyboard(get_groups_order(context)),
            )
            return

        logger.info(
            "Частичная выгрузка OK: файл=%s, правил_групп=%d, позиций=%d, chat_id=%s",
            os.path.basename(path),
            len(rules),
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
            "❌ Файл остатков не найден.\n\nСначала «Актуализировать остатки».",
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

    raw_text = msg.text.strip()
    text = normalize_reply_button_text(raw_text)

    if text == normalize_reply_button_text(START_BUTTON_TEXT):
        return

    menu_n = normalize_reply_button_text(LIMIT_REPLY_MENU_TEXT)
    cancel_n = normalize_reply_button_text(PARTIAL_REPLY_CANCEL_TEXT)
    step = get_gl_wizard_step(context)

    if step == GroupLimitWizardStep.AWAITING_LIMIT_TEXT:
        idx = get_gl_pending_group_index(context)
        gname = gl_group_name_by_index(context, idx) if idx is not None else None
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
            lim = int(raw_text)
            if lim < 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("Введите целое число ≥ 0.")
            raise ApplicationHandlerStop

        set_limit(gname, lim)
        clear_gl_pending_group_index(context)
        set_gl_wizard_step(context, GroupLimitWizardStep.GROUPS_LIST)
        await msg.reply_text(
            f"✅ Для группы <b>{html_escape(gname)}</b> установлен лимит "
            f"<code>{lim}</code> шт.\n"
            "Выберите другую группу или нажмите «В меню».",
            parse_mode=ParseMode.HTML,
            reply_markup=groups_reply_keyboard(
                get_gl_groups_order(context),
                footer_done_text=LIMIT_REPLY_MENU_TEXT,
            ),
        )
        raise ApplicationHandlerStop

    if text == menu_n:
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

    if text == cancel_n:
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

    idx = gl_group_index_by_reply_label(context, raw_text)
    if idx is None:
        await msg.reply_text(
            "Не понял. Выберите группу с клавиатуры или «В меню» / «Отмена».",
        )
        raise ApplicationHandlerStop

    gname = gl_group_name_by_index(context, idx)
    if gname is None:
        await msg.reply_text("Группа не найдена. Отправьте /start.")
        raise ApplicationHandlerStop

    set_gl_wizard_step(context, GroupLimitWizardStep.LIMIT_FOR_GROUP)
    current = get_limit(gname)
    if current is not None:
        cur_line = f"Текущий сохранённый лимит: <code>{current}</code> шт."
    else:
        cur_line = "Лимит пока <b>не задан</b>."

    await msg.reply_text(
        f"Группа: <b>{html_escape(gname)}</b>\n{cur_line}\n\nВыберите лимит:",
        parse_mode=ParseMode.HTML,
        reply_markup=group_limit_threshold_inline_keyboard(idx),
    )
    raise ApplicationHandlerStop


def _red_bin_id_for_key(key: str) -> str | None:
    """
    Id записи Red Bin с тем же K1 или ``None``.

    :param key: Ключ :func:`services.zero_sold.make_position_key`.
    :type key: str
    :returns: Id или ``None``.
    :rtype: str | None
    """
    for item_id, rec in list_red_bin().items():
        if rec.get("key") == key:
            return item_id
    return None


def _red_bin_match_at(context: ContextTypes.DEFAULT_TYPE, index: int) -> dict | None:
    """
    Строка последней выдачи поиска по индексу callback.

    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :param index: Индекс ``rb:<idx>``.
    :type index: int
    :returns: Словарь совпадения или ``None``.
    :rtype: dict | None
    """
    matches = get_red_bin_matches(context)
    if index < 0 or index >= len(matches):
        return None
    match = matches[index]
    return match if isinstance(match, dict) else None


def _red_bin_fields(match: dict) -> dict:
    """
    Поля для :func:`storage.data_store.upsert_red_bin` из строки поиска.

    :param match: Совпадение с ``key``, ``group``, ``brand``, ``name``, ``quantity``.
    :type match: dict
    :returns: Словарь с ``last_qty``.
    :rtype: dict
    """
    return {
        "key": match["key"],
        "group": match.get("group") or "",
        "brand": match.get("brand") or "",
        "name": match.get("name") or "",
        "last_qty": int(match["quantity"]),
    }


def _red_bin_saved_text(name: str, limit: int, qty: int) -> str:
    """
    Подтверждение установки лимита без отправки предупреждения.

    :param name: Название позиции.
    :type name: str
    :param limit: Установленный лимит.
    :type limit: int
    :param qty: Остаток на момент выбора.
    :type qty: int
    :returns: HTML.
    :rtype: str
    """
    return (
        f"✅ <b>{html_escape(name)}</b>: лимит <code>{limit}</code>, "
        f"сейчас <code>{qty}</code>.\n"
        f"Дальше: на {limit} / ниже {limit} / на 0."
    )


def _red_bin_catalog() -> list[dict]:
    """
    Текущий снимок остатков для поиска и списка.

    :returns: Позиции ``parse_stock``.
    :rtype: list[dict]
    :raises FileNotFoundError: Нет ``current.xlsx``.
    """
    from parser.excel_parser import parse_stock

    return parse_stock()


def _red_bin_report_rows() -> list[dict]:
    """
    Все позиции Red Bin с лимитом и остатком из текущего файла.

    :returns: Строки для чата и xlsx.
    :rtype: list[dict]
    """
    from services.red_bin import index_snapshot, red_bin_report_rows

    stock: dict = {}
    if os.path.isfile(config.CURRENT_XLSX_PATH):
        try:
            stock = index_snapshot(_red_bin_catalog())
        except Exception:
            logger.error("Red Bin: снимок для выгрузки не прочитан.", exc_info=True)
    return red_bin_report_rows(list_red_bin(), stock)


def _red_bin_page_title(title: str, total: int, page: int) -> str:
    """
    Заголовок уровня каталога. Номер страницы добавляется, только если их больше одной.

    :param title: HTML-заголовок уровня.
    :type title: str
    :param total: Сколько кнопок на уровне.
    :type total: int
    :param page: Страница с нуля.
    :type page: int
    :returns: Заголовок, возможно с ``· 2/5``.
    :rtype: str
    """
    pages = max(1, (total + RED_BIN_PAGE_SIZE - 1) // RED_BIN_PAGE_SIZE) if total else 1
    if pages > 1:
        shown = min(max(page, 0), pages - 1) + 1
        return f"{title} · {shown}/{pages}"
    return title


def _red_bin_groups_view(context: ContextTypes.DEFAULT_TYPE, page: int) -> tuple[str, object]:
    """
    Текст и кнопки групп.

    :param context: Контекст хэндлера с уже записанным каталогом.
    :type context: ContextTypes.DEFAULT_TYPE
    :param page: Страница с нуля.
    :type page: int
    :returns: ``(html, клавиатура)``.
    :rtype: tuple[str, object]
    :side effects: Шаг сессии становится ``GROUPS``.
    """
    groups = list(get_red_bin_tree(context))
    set_red_bin_step(context, RedBinStep.GROUPS)
    return (
        _red_bin_page_title("Выберите группу", len(groups), page),
        red_bin_browse_keyboard("g", groups, page, back=False),
    )


def _red_bin_brands_view(context: ContextTypes.DEFAULT_TYPE, page: int) -> tuple[str, object] | None:
    """
    Текст и кнопки брендов выбранной группы.

    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :param page: Страница с нуля.
    :type page: int
    :returns: Пара для сообщения или ``None``, если группа в каталоге пропала.
    :rtype: tuple[str, object] | None
    :side effects: Шаг сессии становится ``BRANDS``.
    """
    tree = get_red_bin_tree(context)
    group = get_red_bin_group(context)
    if not group or group not in tree:
        return None
    brands = list(tree[group])
    set_red_bin_step(context, RedBinStep.BRANDS)
    title = f"<b>{html_escape(group)}</b>"
    return (
        _red_bin_page_title(title, len(brands), page),
        red_bin_browse_keyboard("b", brands, page, back=True),
    )


def _red_bin_lines(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """
    Бренды indent 10 текущего раздела или ``None``, если шаг потерял родителей.

    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: ``имя бренда → список моделей``.
    :rtype: dict | None
    """
    tree = get_red_bin_tree(context)
    group = get_red_bin_group(context)
    section = get_red_bin_brand(context)
    if not group or not section or group not in tree or section not in tree[group]:
        return None
    lines = tree[group][section]
    return lines if isinstance(lines, dict) else None


def _red_bin_lines_view(context: ContextTypes.DEFAULT_TYPE, page: int) -> tuple[str, object] | None:
    """
    Кнопки брендов (indent 10): Jogel, Mikasa.

    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :param page: Страница с нуля.
    :type page: int
    :returns: Пара для сообщения или ``None``.
    :rtype: tuple[str, object] | None
    :side effects: Шаг ``LINES``.
    """
    lines = _red_bin_lines(context)
    if lines is None:
        return None
    names = list(lines)
    group = get_red_bin_group(context) or ""
    section = get_red_bin_brand(context) or ""
    set_red_bin_step(context, RedBinStep.LINES)
    title = f"<b>{html_escape(group)}</b> → <b>{html_escape(section)}</b>\nВыберите бренд"
    return (
        _red_bin_page_title(title, len(names), page),
        red_bin_browse_keyboard("l", names, page, back=True),
    )


def _red_bin_line_models(context: ContextTypes.DEFAULT_TYPE) -> list | None:
    """
    Модели выбранного бренда.

    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: Список позиций или ``None``.
    :rtype: list | None
    """
    lines = _red_bin_lines(context)
    line = get_red_bin_line(context)
    if lines is None or not line or line not in lines:
        return None
    models = lines[line]
    return list(models) if isinstance(models, list) else None


def _red_bin_products_view(context: ContextTypes.DEFAULT_TYPE, page: int) -> tuple[str, object] | None:
    """
    Кнопки моделей выбранного бренда (indent 12).

    Список кладётся в сессию целиком: callback несёт индекс, не имя.

    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :param page: Страница с нуля.
    :type page: int
    :returns: Пара для сообщения или ``None``, если бренд в каталоге пропал.
    :rtype: tuple[str, object] | None
    :side effects: Шаг ``PRODUCTS`` и :func:`set_red_bin_matches`.
    """
    products = _red_bin_line_models(context)
    if products is None:
        return None
    set_red_bin_matches(context, products)
    set_red_bin_step(context, RedBinStep.PRODUCTS)
    labels = [f"{row.get('name') or ''} · {row.get('quantity')}" for row in products]
    line = get_red_bin_line(context) or ""
    title = f"<b>{html_escape(line)}</b>\nВыберите модель"
    return (
        _red_bin_page_title(title, len(labels), page),
        red_bin_browse_keyboard("p", labels, page, back=True),
    )


async def _edit_red_bin_level(query, text: str, markup: object) -> None:
    """
    Меняет сообщение каталога Red Bin на другой уровень или страницу.

    :param query: CallbackQuery.
    :type query: telegram.CallbackQuery
    :param text: HTML-текст.
    :type text: str
    :param markup: Inline-клавиатура уровня.
    :type markup: object
    :returns: None
    """
    await _edit_callback_message_safe(
        query,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def cb_red_bin_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Вход в Red Bin: сразу кнопки групп, дальше бренд и товар.

    :param update: Callback кнопки «Red Bin».
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: :func:`init_red_bin_session`, каталог в ``user_data``, сообщения в чат.
    """
    from services.red_bin import browse_tree

    query = update.callback_query
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return
    if not _check_auth(query.message.chat_id):
        await _edit_callback_message_safe(query, _NOT_AUTH_TEXT)
        return
    if get_filter_mode(context) == FilterMode.RED_BIN:
        await _edit_callback_message_safe(
            query,
            "🔴 *Red Bin* уже открыт.\n\nЖмите кнопки в чате или «В меню».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        return
    if not os.path.isfile(config.CURRENT_XLSX_PATH):
        await _edit_callback_message_safe(
            query,
            "📭 Нет файла остатков.\n\nСначала нажмите «Актуализировать остатки».",
            reply_markup=main_keyboard(),
        )
        return
    try:
        tree = browse_tree(_red_bin_catalog())
    except Exception:
        logger.error("Red Bin: каталог не прочитан.", exc_info=True)
        await _edit_callback_message_safe(
            query,
            "❌ Не удалось прочитать таблицу остатков.\n\nПопробуйте позже.",
            reply_markup=main_keyboard(),
        )
        return
    if not tree:
        await _edit_callback_message_safe(
            query,
            "📭 В файле нет товарных позиций.\n\nАктуализируйте остатки.",
            reply_markup=main_keyboard(),
        )
        return
    init_red_bin_session(context)
    set_red_bin_tree(context, tree)
    text, markup = _red_bin_groups_view(context, 0)
    await _edit_callback_message_safe(
        query,
        "🔴 *Red Bin*\n\n"
        "Лимит на *одну модель*. Кнопками: группа → бренд → модель, "
        "затем `10` / `5` / `3` или свой.\n"
        "*«Назад»* — на уровень выше. *«Список»* и *«В меню»* — внизу.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="«Список» и «В меню» — на клавиатуре внизу.",
        reply_markup=red_bin_reply_keyboard(),
    )


def _red_bin_limit_text(match: dict) -> str:
    """
    Текст карточки лимита: имя, остаток, текущий порог если уже стоит.

    :param match: Строка каталога с ``key``, ``name``, ``quantity``.
    :type match: dict
    :returns: HTML.
    :rtype: str
    """
    watched = _red_bin_id_for_key(str(match["key"]))
    extra = ""
    if watched:
        current = list_red_bin().get(watched) or {}
        extra = f"\nУже в корзине, лимит <code>{current.get('limit')}</code>."
    return (
        f"<b>{html_escape(str(match.get('name') or ''))}</b> · "
        f"сейчас <code>{match.get('quantity')}</code>.{extra}\n\nВыберите лимит:"
    )


async def _red_bin_export(query, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """
    Выгружает все лимиты Red Bin в чат или xlsx.

    :param query: Callback «В чат» / «Скачать файл».
    :type query: telegram.CallbackQuery
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :param kind: ``chat`` или ``file``.
    :type kind: str
    :returns: None
    :side effects: Сообщения или документ в чат, временный xlsx удаляется.
    """
    import tempfile

    from services.red_bin import format_red_bin_report_html, write_red_bin_xlsx

    rows = _red_bin_report_rows()
    if not rows:
        await query.edit_message_text("Red Bin пуст.")
        return
    if kind == "chat":
        messages = format_red_bin_report_html(rows)
        await query.edit_message_text(messages[0], parse_mode=ParseMode.HTML)
        for extra in messages[1:]:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=extra,
                parse_mode=ParseMode.HTML,
            )
        return

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        write_red_bin_xlsx(rows, path)
        await query.edit_message_text(f"Файл: {len(rows)} поз. с лимитом.")
        with open(path, "rb") as handle:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=handle,
                filename="red_bin_лимиты.xlsx",
                caption=f"Red Bin: {len(rows)} поз. с лимитом",
            )
    except Exception:
        logger.error("Red Bin: не удалось отправить xlsx.", exc_info=True)
        await query.edit_message_text("Не удалось собрать файл. Попробуйте ещё раз.")
    finally:
        if os.path.isfile(path):
            os.remove(path)


async def cb_red_bin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Callback каталога Red Bin: группа, бренд, товар, лимит, снятие.

    :param update: Callback ``rb:``.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :side effects: ``storage/red_bin.json``, правка сообщения.
    """
    query = update.callback_query
    if not await _answer_callback_query_safe(query):
        await _notify_telegram_network_unreachable(context, query)
        return
    if not _check_auth(query.message.chat_id):
        await query.edit_message_text(_NOT_AUTH_TEXT)
        return
    if get_filter_mode(context) != FilterMode.RED_BIN:
        await query.edit_message_text("Сессия устарела. Отправьте /start.")
        return
    parsed = parse_red_bin_callback(query.data or "")
    if not parsed:
        return

    if parsed[0] == "clear_id":
        clear_red_bin(parsed[1])
        left = len(list_red_bin())
        await query.edit_message_text(f"Снято. В Red Bin осталось {left}.")
        return

    if parsed[0] == "export":
        await _red_bin_export(query, context, parsed[1])
        return

    if parsed[0] == "page":
        level, page = parsed[1], int(parsed[2])
        view = {
            "g": _red_bin_groups_view,
            "b": _red_bin_brands_view,
            "l": _red_bin_lines_view,
            "p": _red_bin_products_view,
        }[level](context, page)
        if view is None:
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        await _edit_red_bin_level(query, view[0], view[1])
        return

    if parsed[0] == "group":
        groups = list(get_red_bin_tree(context))
        idx = int(parsed[1])
        if idx < 0 or idx >= len(groups):
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        set_red_bin_group(context, groups[idx])
        view = _red_bin_brands_view(context, 0)
        if view is None:
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        await _edit_red_bin_level(query, view[0], view[1])
        return

    if parsed[0] == "brand":
        group = get_red_bin_group(context)
        brands = list(get_red_bin_tree(context).get(group or "", {}))
        idx = int(parsed[1])
        if idx < 0 or idx >= len(brands):
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        set_red_bin_brand(context, brands[idx])
        view = _red_bin_lines_view(context, 0)
        if view is None:
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        await _edit_red_bin_level(query, view[0], view[1])
        return

    if parsed[0] == "line":
        lines = _red_bin_lines(context) or {}
        names = list(lines)
        idx = int(parsed[1])
        if idx < 0 or idx >= len(names):
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        line_name = names[idx]
        set_red_bin_line(context, line_name)
        models = list(lines[line_name])
        has_models = any(int(row.get("indent") or 10) >= 12 for row in models)
        if not has_models and len(models) == 1:
            set_red_bin_matches(context, models)
            set_red_bin_step(context, RedBinStep.LIMIT)
            await query.edit_message_text(
                _red_bin_limit_text(models[0]),
                parse_mode=ParseMode.HTML,
                reply_markup=red_bin_limit_keyboard(0),
            )
            return
        view = _red_bin_products_view(context, 0)
        if view is None:
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        await _edit_red_bin_level(query, view[0], view[1])
        return

    if parsed[0] == "up":
        step = get_red_bin_step(context)
        if step == RedBinStep.AWAITING_LIMIT:
            index = get_red_bin_pending_index(context)
            match = _red_bin_match_at(context, index) if index is not None else None
            if match is None or index is None:
                view = _red_bin_products_view(context, 0)
            else:
                set_red_bin_step(context, RedBinStep.LIMIT)
                await query.edit_message_text(
                    _red_bin_limit_text(match),
                    parse_mode=ParseMode.HTML,
                    reply_markup=red_bin_limit_keyboard(index),
                )
                return
        elif step == RedBinStep.LIMIT:
            clear_red_bin_pending_index(context)
            models = _red_bin_line_models(context) or []
            has_models = any(int(row.get("indent") or 10) >= 12 for row in models)
            view = _red_bin_products_view(context, 0) if has_models else _red_bin_lines_view(context, 0)
        elif step == RedBinStep.PRODUCTS:
            view = _red_bin_lines_view(context, 0)
        elif step == RedBinStep.LINES:
            view = _red_bin_brands_view(context, 0)
        else:
            view = _red_bin_groups_view(context, 0)
        if view is None:
            await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
            return
        await _edit_red_bin_level(query, view[0], view[1])
        return

    index = int(parsed[1])
    match = _red_bin_match_at(context, index)
    if match is None:
        await query.edit_message_text("Список устарел. Откройте Red Bin заново.")
        return

    if parsed[0] == "pick":
        set_red_bin_step(context, RedBinStep.LIMIT)
        await query.edit_message_text(
            _red_bin_limit_text(match),
            parse_mode=ParseMode.HTML,
            reply_markup=red_bin_limit_keyboard(index),
        )
        return

    value = parsed[2]
    if value == "clear":
        watched = _red_bin_id_for_key(str(match["key"]))
        if watched:
            clear_red_bin(watched)
            note = f"🗑 <b>{html_escape(str(match.get('name') or ''))}</b> снят с Red Bin."
        else:
            note = "Лимит для этой позиции не был задан."
        clear_red_bin_pending_index(context)
        view = _red_bin_products_view(context, 0)
        if view is None:
            await query.edit_message_text(note, parse_mode=ParseMode.HTML)
            return
        await _edit_red_bin_level(query, note + "\n\n" + view[0], view[1])
        return

    if value == "custom":
        set_red_bin_pending_index(context, index)
        set_red_bin_step(context, RedBinStep.AWAITING_LIMIT)
        await query.edit_message_text(
            f"✏️ <b>{html_escape(str(match.get('name') or ''))}</b>\n\n"
            "Введите целое число ≥ 1 — сколько штук держать в корзине.",
            parse_mode=ParseMode.HTML,
            reply_markup=red_bin_browse_keyboard("p", [], 0, back=True),
        )
        return

    if isinstance(value, int):
        upsert_red_bin(_red_bin_fields(match), value)
        clear_red_bin_pending_index(context)
        note = _red_bin_saved_text(str(match.get("name") or ""), value, int(match["quantity"]))
        view = _red_bin_products_view(context, 0)
        if view is None:
            await query.edit_message_text(note, parse_mode=ParseMode.HTML)
            return
        await _edit_red_bin_level(query, note + "\n\n" + view[0], view[1])


async def handle_red_bin_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Текст в режиме Red Bin: свой лимит, список надзора, выход в меню.

    Название текстом больше не ищется — товар выбирается inline-кнопками.

    :param update: Текстовое сообщение.
    :type update: Update
    :param context: Контекст хэндлера.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :raises ApplicationHandlerStop: Сообщение обработано здесь.
    """
    msg = _text_message(update)
    if not msg or not msg.text:
        return
    if not _check_auth(update.effective_chat.id):
        return
    if get_filter_mode(context) != FilterMode.RED_BIN:
        return

    raw_text = msg.text.strip()
    text = normalize_reply_button_text(raw_text)
    list_n = normalize_reply_button_text(RED_BIN_LIST_TEXT)
    menu_n = normalize_reply_button_text(LIMIT_REPLY_MENU_TEXT)

    if text == menu_n:
        reset_session_to_global(context)
        await msg.reply_text("Red Bin закрыт.", reply_markup=start_keyboard())
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Выберите действие:",
            reply_markup=main_keyboard(),
        )
        raise ApplicationHandlerStop

    if text == list_n:
        rows = _red_bin_report_rows()
        if not rows:
            await msg.reply_text("Red Bin пуст.")
            raise ApplicationHandlerStop
        await msg.reply_text(
            f"Позиций с лимитом: {len(rows)}. Куда вывести?",
            reply_markup=red_bin_export_keyboard(),
        )
        raise ApplicationHandlerStop

    if get_red_bin_step(context) == RedBinStep.AWAITING_LIMIT:
        index = get_red_bin_pending_index(context)
        match = _red_bin_match_at(context, index) if index is not None else None
        if match is None:
            clear_red_bin_pending_index(context)
            await msg.reply_text("Сессия ввода сброшена. Выберите товар кнопками ещё раз.")
            raise ApplicationHandlerStop
        try:
            limit = int(raw_text)
            if limit < 1:
                raise ValueError
        except ValueError:
            await msg.reply_text("Введите целое число ≥ 1.")
            raise ApplicationHandlerStop
        upsert_red_bin(_red_bin_fields(match), limit)
        clear_red_bin_pending_index(context)
        note = _red_bin_saved_text(str(match.get("name") or ""), limit, int(match["quantity"]))
        view = _red_bin_products_view(context, 0)
        await msg.reply_text(note, parse_mode=ParseMode.HTML)
        if view is not None:
            await msg.reply_text(view[0], parse_mode=ParseMode.HTML, reply_markup=view[1])
        raise ApplicationHandlerStop

    await msg.reply_text("Выберите группу, бренд и товар кнопками в чате.")
    raise ApplicationHandlerStop


async def handle_group_and_partial_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Текстовые мастера «лимиты по группам» и «частичная фильтрация» в одной цепочке.

    В python-telegram-bot v20 в пределах одной группы хэндлеров срабатывает
    не более **одного** ``MessageHandler`` на обновление (см. ``break`` в
    ``Application.process_update``). Два отдельных хэндлера с одинаковым
    фильтром ``filters.TEXT`` приводили к тому, что первый
    (``handle_group_limit_messages``) всегда «съедал» сообщение и при режиме
    ``PARTIAL_FILTER`` тихо выходил — частичная фильтрация не получала текст.

    :param update: Входящее обновление с текстом.
    :type update: Update
    :param context: Контекст PTB.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    :raises ApplicationHandlerStop: Если один из внутренних хэндлеров
                                      остановил цепочку.
    """
    try:
        await handle_partial_messages(update, context)
    except ApplicationHandlerStop:
        raise
    try:
        await handle_group_limit_messages(update, context)
    except ApplicationHandlerStop:
        raise
    try:
        await handle_red_bin_messages(update, context)
    except ApplicationHandlerStop:
        raise


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

    if get_filter_mode(context) == FilterMode.RED_BIN:
        return

    chat_id = update.effective_chat.id
    raw_text = msg.text.strip()
    text = normalize_reply_button_text(raw_text)
    mode = get_filter_mode(context)

    apply_n = normalize_reply_button_text(PARTIAL_REPLY_APPLY_TEXT)
    cancel_n = normalize_reply_button_text(PARTIAL_REPLY_CANCEL_TEXT)

    if mode != FilterMode.PARTIAL_FILTER:
        if text == apply_n or text == cancel_n:
            if mode == FilterMode.GROUP_LIMIT_SETUP:
                await msg.reply_text(
                    "Сейчас открыт мастер *лимитов по группам*. "
                    "Кнопки «Применить» / «Отмена» относятся к *частичной фильтрации*.\n\n"
                    "Завершите настройку лимитов кнопкой «✅ В меню» или «❌ Отмена», "
                    "затем снова выберите «🔀 Частичная фильтрация».",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=groups_reply_keyboard(
                        get_gl_groups_order(context),
                        footer_done_text=LIMIT_REPLY_MENU_TEXT,
                    ),
                )
            else:
                await msg.reply_text(
                    "⚠️ Режим «частичной фильтрации» не активен "
                    "(сессия сбросилась: перезапуск бота, /start, актуализация и т.п.).\n\n"
                    "Нажмите «🔀 Частичная фильтрация» в меню — клавиатура с группами "
                    "обновится.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Выберите действие:",
                    reply_markup=main_keyboard(),
                )
            raise ApplicationHandlerStop
        if mode == FilterMode.GLOBAL_FILTER and is_known_product_group_reply_text(
            raw_text,
        ):
            await msg.reply_text(
                "⚠️ Видна reply-клавиатура с группами, но сценарий "
                "«частичной фильтрации» не активен (например после /start или "
                "перезапуска бота).\n\n"
                "Клавиатура снята. Нажмите «🔀 Частичная фильтрация» в меню ниже.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="Выберите действие:",
                reply_markup=main_keyboard(),
            )
            raise ApplicationHandlerStop
        return

    if text == normalize_reply_button_text(START_BUTTON_TEXT):
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
            f"✅ <b>{html_escape(gname)}</b>: порог ≤ <code>{thr}</code> шт.\n"
            "Выберите следующую группу или «Применить».",
            parse_mode=ParseMode.HTML,
            reply_markup=groups_reply_keyboard(get_groups_order(context)),
        )
        raise ApplicationHandlerStop

    if text == apply_n:
        await _partial_apply(update, context)
        raise ApplicationHandlerStop

    if text == cancel_n:
        await _partial_cancel(update, context)
        raise ApplicationHandlerStop

    idx = group_index_by_reply_label(context, raw_text)
    if idx is None:
        await msg.reply_text(
            "Не понял. Выберите группу с клавиатуры или «Применить» / «Отмена».",
        )
        raise ApplicationHandlerStop

    gname = group_name_by_index(context, idx)
    if gname is None:
        await msg.reply_text("Группа не найдена. Отправьте /start.")
        raise ApplicationHandlerStop

    set_wizard_step(context, PartialWizardStep.THRESHOLD_FOR_GROUP)
    await msg.reply_text(
        f"Группа: <b>{html_escape(gname)}</b>\nВыберите порог остатка для этой группы (частичная выгрузка Excel):",
        parse_mode=ParseMode.HTML,
        reply_markup=partial_threshold_inline_keyboard(idx),
    )
    raise ApplicationHandlerStop


def register_handlers(app: Application) -> None:
    """
    Регистрирует все хэндлеры команд, callback-кнопок и текстовых сообщений.

    Порядок регистрации важен:
    1. Команды (/start, /help) — первыми, чтобы telegram не путал с текстом.
    2. Callback: ``rb:`` / Red Bin, ``glt:`` и ``group_limit_start``, частичная
       фильтрация (``pft:``, ``partial_start``), затем актуализация и общий Excel.
    3. Reply «▶️ Start» — до мастеров и пароля.
    4. Текст: ``handle_group_and_partial_text`` (частичная фильтрация, лимиты
       групп и Red Bin подряд; из-за ограничения PTB v20 — один ``MessageHandler``).
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
    app.add_handler(CallbackQueryHandler(cb_red_bin, pattern=r"^rb:"))
    app.add_handler(
        CallbackQueryHandler(cb_red_bin_start, pattern=f"^{CB_RED_BIN_START}$"),
    )
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
            handle_group_and_partial_text,
        ),
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    logger.info("Хэндлеры зарегистрированы.")
