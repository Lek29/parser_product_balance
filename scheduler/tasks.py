"""
Планировщик задач приложения.

Использует APScheduler (BackgroundScheduler) для еженедельного автоматического
получения и обработки остатков с корпоративной почты, очистки IMAP-папки Stock
и ревизии порогов по группам (``docs/fourty_role.md``, этап 7а).

Расписание (Europe/Moscow):

* понедельник 09:00 — полный цикл почты и парсинга (:func:`fetch_and_parse`);
* понедельник 09:30 — очистка лишних писем в Stock (:func:`weekly_stock_mail_cleanup`);
* четверг 10:00 — ревизия лимитов (:func:`weekly_group_limit_revision`).

Частичная выгрузка (``filtered_partial.xlsx``) планировщиком **не** создаётся:
пишется только глобальный ``filtered.xlsx`` (см. :func:`fetch_and_parse`).
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
    5. Генерирует глобальный отфильтрованный файл (``config.FILTERED_XLSX_PATH``).

    Файл частичной фильтрации (``FILTERED_PARTIAL_XLSX_PATH``) не затрагивается.

    :returns: None
    :side effects: Обновляет файлы storage/current.xlsx, storage/meta.json,
                   storage/data.json, глобальный filtered.xlsx.
    """
    logger.info("Планировщик: запуск задачи fetch_and_parse.")

    try:
        from parser.mail_fetcher import MailFetchOutcome, fetch_and_save

        outcome = fetch_and_save()

        if outcome == MailFetchOutcome.SKIPPED_TODAY:
            logger.info("Планировщик: IMAP пропущен — локальный снимок свеж (календарная неделя МСК).")
            return

        if outcome == MailFetchOutcome.ARCHIVE_FAILURE:
            logger.error(
                "Планировщик: MAIL_FETCH_ARCHIVE_FAILURE — скачивание или "
                "распаковка архива не удались; парсинг не выполняется."
            )
            return

        # NO_CHANGES / UPDATED: парсим локальный xlsx (нужно для zero-sold на границе недели).
        # shortcut: при SKIPPED_TODAY выше return — в начале новой недели skip не срабатывает.
        from services.stock_pipeline import run_parse_and_save_filtered

        if outcome == MailFetchOutcome.NO_CHANGES:
            logger.info("Планировщик: IMAP без новых писем — локальный parse.")

        all_count, filtered_count = run_parse_and_save_filtered()

        logger.info(
            "Планировщик: обновление завершено. Всего позиций: %d, "
            "с малым остатком: %d. (Частичный filtered_partial не пишется.)",
            all_count,
            filtered_count,
        )

    except Exception as e:
        logger.error("Планировщик: ошибка при выполнении fetch_and_parse: %s", e, exc_info=True)


def weekly_group_limit_revision() -> None:
    """
    Задача планировщика: еженедельная ревизия порогов по группам под надзором.

    Делегирует :func:`services.group_limit_revision.run_weekly_group_limit_revision`.
    Ошибки внутри не роняют процесс — см. реализацию модуля ревизии.

    :returns: None
    :rtype: None
    """
    logger.info("Планировщик: запуск задачи weekly_group_limit_revision.")
    try:
        from services.group_limit_revision import run_weekly_group_limit_revision

        run_weekly_group_limit_revision()
    except Exception as e:
        logger.error(
            "Планировщик: ошибка при weekly_group_limit_revision: %s",
            e,
            exc_info=True,
        )


def weekly_stock_mail_cleanup() -> None:
    """
    Задача планировщика: очистка IMAP-папки Stock (реально удаляет лишнее).

    Делегирует :func:`services.stock_mail_cleanup.run_stock_mail_cleanup`
    с ``execute=True``. Запускается после еженедельного fetch, чтобы keeper
    уже мог быть свежим письмом недели.

    :returns: None
    :rtype: None
    :side effects: Удаляет письма на IMAP-сервере.
    """
    logger.info("Планировщик: запуск задачи weekly_stock_mail_cleanup.")
    try:
        from services.stock_mail_cleanup import run_stock_mail_cleanup

        deleted = run_stock_mail_cleanup(execute=True)
        logger.info(
            "Планировщик: очистка Stock завершена, удалено писем: %d.",
            deleted,
        )
    except Exception as e:
        logger.error(
            "Планировщик: ошибка при weekly_stock_mail_cleanup: %s",
            e,
            exc_info=True,
        )


def create_scheduler() -> BackgroundScheduler:
    """
    Создаёт и настраивает экземпляр BackgroundScheduler с еженедельными задачами.

    Расписание (Europe/Moscow): пн 09:00 fetch, пн 09:30 cleanup Stock,
    чт 10:00 ревизия лимитов. Планировщик в фоновом потоке, не блокирует бота.

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

    scheduler.add_job(
        func=weekly_stock_mail_cleanup,
        trigger=CronTrigger(
            day_of_week="mon",
            hour=9,
            minute=30,
            timezone="Europe/Moscow",
        ),
        id="stock_mail_cleanup_weekly",
        name="Еженедельная очистка IMAP Stock",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        func=weekly_group_limit_revision,
        trigger=CronTrigger(
            day_of_week="thu",
            hour=10,
            minute=0,
            timezone="Europe/Moscow",
        ),
        id="group_limit_revision_weekly",
        name="Еженедельная ревизия порогов по группам",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info(
        "Планировщик создан. Задачи: пн 09:00 — остатки с почты; "
        "пн 09:30 — очистка Stock; "
        "чт 10:00 — ревизия лимитов по группам (этап 7а).",
    )
    return scheduler
