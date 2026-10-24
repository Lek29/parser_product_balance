"""
Общие фикстуры pytest.

Выставляет обязательные переменные окружения до импорта ``config`` в тестах.
Нужно для CI (нет ``.env``) и для локального запуска без файла окружения.
Сначала читается ``.env`` (если есть), затем недостающие ключи заполняются dummy.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Только то, что config._require требует при импорте модуля.
_CI_DUMMY_ENV: dict[str, str] = {
    "EMAIL_ADDRESS": "ci@example.com",
    "EMAIL_PASSWORD": "ci-password",
    "EMAIL_IMAP_SERVER": "imap.example.com",
    "TELEGRAM_BOT_TOKEN": "0:CI_DUMMY_TOKEN",
    "BOT_PASSWORD": "ci-bot-password",
}

for _key, _value in _CI_DUMMY_ENV.items():
    os.environ.setdefault(_key, _value)
