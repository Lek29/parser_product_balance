"""
Точка входа приложения.

Запускает планировщик задач APScheduler в фоновом потоке и Telegram-бота
в режиме polling (python-telegram-bot, HTTPS к Bot API). Корректно завершает
работу при Ctrl+C.
"""

import logging

from telegram import BotCommand, Update
from telegram.ext import Application, ContextTypes

import config
from bot.handlers import register_handlers
from bot.telegram_bridge import register_polling_bridge
from scheduler.tasks import create_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


async def _log_errors(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Логирует необработанные исключения из хэндлеров (без падения процесса).

    :param update: Объект Update или ``None`` (например при ошибке вне хэндлера).
    :type update: object
    :param context: Контекст PTB с полем ``error``.
    :type context: ContextTypes.DEFAULT_TYPE
    :returns: None
    """
    err = context.error
    logger.error(
        "Необработанное исключение в хэндлере: %s",
        err,
        exc_info=err,
    )
    if update is not None:
        logger.debug("update_id при ошибке: %s", getattr(update, "update_id", update))


async def post_init(app: Application) -> None:
    """
    Вызывается после инициализации Application перед запуском polling.

    Регистрирует мост для алертов из фоновых потоков, затем команды бота
    в меню Telegram (кнопка ☰ Menu слева от поля ввода).

    :param app: Экземпляр Application из python-telegram-bot.
    :type app: Application
    :returns: None
    """
    register_polling_bridge(app)
    me = await app.bot.get_me()
    logger.info(
        "Telegram: бот @%s (id=%s), can_join_groups=%s",
        me.username,
        me.id,
        me.can_join_groups,
    )
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить / Главное меню"),
        BotCommand("help",  "Справка по боту"),
    ])
    logger.info("Команды бота зарегистрированы в меню Telegram.")


def main() -> None:
    """
    Главная функция приложения.

    Запускает BackgroundScheduler в фоне, затем запускает Telegram-бота
    в режиме polling. Блокирует выполнение до Ctrl+C, после чего
    корректно останавливает планировщик.

    :returns: None
    """
    logger.info("Запуск приложения...")
    if config.STOCK_LIMIT_ALERT_CHAT_ID is None:
        logger.info(
            "STOCK_LIMIT_ALERT_CHAT_ID не задан — уведомления о порогах по группам отключены.",
        )
    else:
        logger.info(
            "Уведомления о порогах по группам включены (chat_id=%s).",
            config.STOCK_LIMIT_ALERT_CHAT_ID,
        )

    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Планировщик запущен.")

    try:
        builder = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .post_init(post_init)
            .read_timeout(60)
            .write_timeout(120)
            .connect_timeout(30)
        )
        app = builder.build()
        app.add_error_handler(_log_errors)
        register_handlers(app)

        logger.info(
            "Бот запущен (python-telegram-bot). "
            "Ожидание сообщений... (Ctrl+C для остановки)",
        )
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            bootstrap_retries=-1,
            stop_signals=None,
        )
    finally:
        scheduler.shutdown()
        logger.info("Планировщик остановлен. Приложение завершено.")


if __name__ == "__main__":
    main()
