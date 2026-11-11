"""Сохранение очереди между запусками (без UI)."""
from __future__ import annotations

import os

from . import paths
from .jsonio import read_json, write_json_atomic
from .media import is_url

MAX_ITEMS = 5000


def save_queue(files: list[str], index: int) -> str | None:
    clean = [f for f in files if isinstance(f, str) and f][:MAX_ITEMS]
    return write_json_atomic(paths.QUEUE_PATH, {"files": clean, "index": max(0, int(index))})


def load_queue(path=None) -> tuple[list[str], int]:
    """-> (файлы, индекс). Исчезнувшие с диска файлы отбрасываются, индекс пересчитывается."""
    data = read_json(path or paths.QUEUE_PATH, {})
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return [], 0
    raw_index = data.get("index", 0)
    raw_index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
    files: list[str] = []
    new_index = 0
    for i, f in enumerate(data["files"][:MAX_ITEMS]):
        if not isinstance(f, str) or not f:
            continue
        if is_url(f) or os.path.exists(f):
            if i < raw_index:
                new_index += 1
            files.append(f)
    return files, min(new_index, max(0, len(files) - 1))
