"""
CLI: очистка IMAP-папки Stock (см. :mod:`services.stock_mail_cleanup`).

По умолчанию — dry-run. Реальное удаление: ``--execute``.

Запуск из корня проекта::

    python scripts/cleanup_stock_mail.py
    python scripts/cleanup_stock_mail.py --execute

Еженедельный автозапуск — задача планировщика (пн 09:30 МСК), не этот скрипт.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

# Корень репо в sys.path: иначе `python scripts/...` не видит пакет `parser`.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cleanup_stock_mail")


def main(argv: Optional[list[str]] = None) -> int:
    """
    Точка входа CLI очистки Stock.

    :param argv: Аргументы без имени программы; ``None`` — ``sys.argv[1:]``.
    :type argv: Optional[list[str]]
    :returns: Код выхода (0 — успех).
    :rtype: int
    """
    parser = argparse.ArgumentParser(
        description="Очистка IMAP-папки Stock: один keeper + удаление старых/лишних.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Реально удалить письма (без флага — только dry-run).",
    )
    args = parser.parse_args(argv)

    import config
    from services.stock_mail_cleanup import run_stock_mail_cleanup

    try:
        run_stock_mail_cleanup(execute=args.execute)
        if not args.execute:
            logger.info(
                "Для удаления: python scripts/cleanup_stock_mail.py --execute"
            )
        return 0
    except Exception:
        logger.exception("Ошибка очистки папки %r", config.EMAIL_IMAP_FOLDER)
        return 1


if __name__ == "__main__":
    sys.exit(main())
