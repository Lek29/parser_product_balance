"""
Точка входа приложения.

Запускает планировщик задач APScheduler в фоновом потоке и Telegram-бота
в режиме polling. Корректно завершает работу при Ctrl+C.
"""

import logging

from telegram import BotCommand
from telegram.ext import Application

import config
from bot.handlers import register_handlers
from scheduler.tasks import create_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


async def post_init(app: Application) -> None:
    """
    Вызывается после инициализации Application перед запуском polling.

    Регистрирует команды бота в меню Telegram (кнопка ☰ Menu слева от поля ввода).
    Пользователь видит список доступных команд с описаниями.

    :param app: Экземпляр Application из python-telegram-bot.
    :type app: Application
    :returns: None
    """
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

    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Планировщик запущен.")

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .read_timeout(60)
        .write_timeout(120)
        .connect_timeout(30)
        .build()
    )
    register_handlers(app)

    logger.info("Бот запущен. Ожидание сообщений... (Ctrl+C для остановки)")
    app.run_polling(stop_signals=None)

    scheduler.shutdown()
    logger.info("Планировщик остановлен. Приложение завершено.")


if __name__ == "__main__":
    main()
