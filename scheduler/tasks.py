"""
Планировщик задач приложения.

Использует APScheduler (BackgroundScheduler) для еженедельного автоматического
получения и обработки остатков с корпоративной почты.

Расписание: каждый понедельник в 09:00 по московскому времени.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def fetch_and_parse() -> None:
    """
    Задача планировщика: полный цикл обновления остатков.

    Выполняет последовательно:
    1. Скачивает последнее письмо с архивом с корпоративной почты.
    2. Если данные уже актуальны — завершает без изменений.
    3. Парсит скачанный xlsx, строит список всех товарных позиций.
    4. Сохраняет полный список в storage/data.json.
    5. Генерирует отфильтрованный storage/filtered.xlsx.

    :returns: None
    :side effects: Обновляет файлы storage/current.xlsx, storage/meta.json,
                   storage/data.json, storage/filtered.xlsx.
    """
    logger.info("Планировщик: запуск задачи fetch_and_parse.")

    try:
        from parser.mail_fetcher import fetch_and_save
        updated = fetch_and_save()

        if updated is None:
            logger.info(
                "Планировщик: IMAP пропущен — почта уже проверялась сегодня."
            )
            return

        if updated is False:
            logger.info("Планировщик: IMAP проверен, новых писем нет.")
            return

        from parser.excel_parser import parse_stock, filter_by_threshold, generate_filtered_xlsx
        from storage.data_store import save_items

        all_items = parse_stock()
        save_items(all_items)

        filtered = filter_by_threshold(all_items)
        generate_filtered_xlsx(filtered)

        logger.info(
            "Планировщик: обновление завершено. Всего позиций: %d, с малым остатком: %d.",
            len(all_items),
            len(filtered),
        )

    except Exception as e:
        logger.error("Планировщик: ошибка при выполнении fetch_and_parse: %s", e, exc_info=True)


def create_scheduler() -> BackgroundScheduler:
    """
    Создаёт и настраивает экземпляр BackgroundScheduler с задачей обновления остатков.

    Расписание: каждый понедельник в 09:00 (timezone Europe/Moscow).
    Планировщик запускается в фоновом потоке и не блокирует основной процесс.

    :returns: Настроенный, но ещё не запущенный экземпляр BackgroundScheduler.
    :rtype: BackgroundScheduler

    :example:
    >>> scheduler = create_scheduler()
    >>> scheduler.start()
    >>> # ... основной цикл приложения ...
    >>> scheduler.shutdown()
    """
    scheduler = BackgroundScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        func=fetch_and_parse,
        trigger=CronTrigger(
            day_of_week="mon",
            hour=9,
            minute=0,
            timezone="Europe/Moscow",
        ),
        id="fetch_and_parse_weekly",
        name="Еженедельное обновление остатков",
        replace_existing=True,
        misfire_grace_time=3600,  # допускаем опоздание до 1 часа
    )

    logger.info(
        "Планировщик создан. Задача: каждый понедельник в 09:00 (МСК)."
    )
    return scheduler
