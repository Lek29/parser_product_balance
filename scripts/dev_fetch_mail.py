"""
Утилита разработчика: один вызов загрузки остатков с почты (без бота).

Запуск из корня проекта::

    python scripts/dev_fetch_mail.py
"""

from __future__ import annotations

if __name__ == "__main__":
    from parser.mail_fetcher import fetch_and_save

    result = fetch_and_save()
    print("Результат:", result.value)
