"""
Выводит содержимое ``storage/meta.json`` (диагностика).

Запуск из корня проекта::

    python scripts/show_meta.py
"""

from __future__ import annotations

import json

if __name__ == "__main__":
    from storage.data_store import read_meta_json

    meta = read_meta_json()
    if meta is None:
        print("meta.json отсутствует или не читается.")
    else:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
