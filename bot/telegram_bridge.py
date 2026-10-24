"""
Мост для отправки сообщений из синхронного кода в цикл polling PTB.

Планировщик и другие потоки вызывают :func:`send_group_limit_alert_html` и
:func:`send_zero_sold_xlsx`; отправка через
:func:`asyncio.run_coroutine_threadsafe` на тот же ``event_loop``, где
работает бот (см. ``docs/fourty_role.md``, ``docs/push_message_about_zero.md``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

import config

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def register_polling_bridge(app: Application) -> None:
    """
    Сохраняет ``Bot`` и ``asyncio``-loop текущего polling для межпоточной отправки.

    Вызывать из ``post_init`` приложения (уже внутри рабочего цикла PTB).

    :param app: Инициализированное приложение python-telegram-bot.
    :type app: Application
    :returns: None
    :side effects: Заполняет модульные ``_bot`` и ``_loop``.
    """
    global _bot, _loop
    _bot = app.bot
    _loop = asyncio.get_running_loop()
    logger.info(
        "Telegram bridge: зарегистрированы bot и event loop для алертов.",
    )


def send_group_limit_alert_html(html: str) -> None:
    """
    Отправляет HTML-алерт о лимитах групп в ``STOCK_LIMIT_ALERT_CHAT_ID``.

    Безопасна при отсутствии чата в конфиге или до старта polling: пишет
    предупреждение в лог и выходит. Ошибки Telegram API логируются с
    ``exc_info``, исключение наружу не пробрасывает.

    :param html: Текст из
                 :func:`services.group_limit_alert_text.format_group_limit_alerts_html`.
    :type html: str
    :returns: None
    :side effects: HTTP-запрос к Bot API в цикле polling.
    """
    if not html:
        return

    chat_id = config.STOCK_LIMIT_ALERT_CHAT_ID
    if chat_id is None:
        logger.warning(
            "Telegram bridge: STOCK_LIMIT_ALERT_CHAT_ID не задан — " "алерт лимитов не отправлен (%d симв. текста).",
            len(html),
        )
        return

    if _bot is None or _loop is None:
        logger.warning(
            "Telegram bridge: bot/loop не зарегистрированы (polling ещё не "
            "готов?) — алерт не отправлен (chat_id=%s, %d симв.).",
            chat_id,
            len(html),
        )
        return

    async def _send() -> None:
        await _bot.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    fut = asyncio.run_coroutine_threadsafe(_send(), _loop)
    try:
        fut.result(timeout=90)
    except Exception as e:
        logger.error(
            "Telegram bridge: ошибка отправки алерта в chat_id=%s: %s",
            chat_id,
            e,
            exc_info=True,
        )
        return

    logger.info(
        "Telegram bridge: алерт лимитов отправлен в chat_id=%s (%d симв.).",
        chat_id,
        len(html),
    )


def send_zero_sold_xlsx(
    path: str,
    *,
    caption: str,
    filename: str,
) -> bool:
    """
    Отправляет xlsx «ушли в ноль» в личку (``STOCK_LIMIT_ALERT_CHAT_ID``).

    :param path: Локальный путь к файлу ``.xlsx``.
    :type path: str
    :param caption: Подпись к документу.
    :type caption: str
    :param filename: Имя файла, видимое в Telegram (с датой).
    :type filename: str
    :returns: ``True``, если отправка завершилась без исключения.
    :rtype: bool
    :side effects: HTTP ``sendDocument`` в цикле polling.
    """
    chat_id = config.STOCK_LIMIT_ALERT_CHAT_ID
    if chat_id is None:
        logger.warning(
            "Telegram bridge: STOCK_LIMIT_ALERT_CHAT_ID не задан — " "zero-sold xlsx не отправлен.",
        )
        return False

    if _bot is None or _loop is None:
        logger.warning(
            "Telegram bridge: bot/loop не зарегистрированы — zero-sold xlsx " "не отправлен (chat_id=%s).",
            chat_id,
        )
        return False

    async def _send() -> None:
        with open(path, "rb") as f:
            await _bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                caption=caption,
                write_timeout=120,
                read_timeout=60,
            )

    fut = asyncio.run_coroutine_threadsafe(_send(), _loop)
    try:
        fut.result(timeout=120)
    except Exception as e:
        logger.error(
            "Telegram bridge: ошибка send_document zero-sold в chat_id=%s: %s",
            chat_id,
            e,
            exc_info=True,
        )
        return False

    logger.info(
        "Telegram bridge: zero-sold xlsx отправлен в chat_id=%s file=%s",
        chat_id,
        filename,
    )
    return True
