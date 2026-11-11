"""Локализация. Строки лежат в locales/<код>.json (+ пользовательские в ~/.config)."""
from __future__ import annotations

from . import paths
from .jsonio import read_json

FALLBACK_LANG = "en"

_tables: dict[str, dict[str, str]] = {}


def _load_dir(directory) -> None:
    if not directory.is_dir():
        return
    for file in sorted(directory.glob("*.json")):
        data = read_json(file)
        if isinstance(data, dict):
            table = _tables.setdefault(file.stem, {})
            table.update({k: v for k, v in data.items() if isinstance(v, str)})


def reload() -> None:
    _tables.clear()
    _load_dir(paths.LOCALES_DIR)
    _load_dir(paths.USER_LOCALES_DIR)


def available_languages() -> list[tuple[str, str]]:
    """[(код, название)] — для выбора языка в настройках."""
    return [(code, table.get("_name", code)) for code, table in sorted(_tables.items())]


def tr(lang: str, key: str, **kwargs) -> str:
    text = _tables.get(lang, {}).get(key)
    if text is None:
        text = _tables.get(FALLBACK_LANG, {}).get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


reload()
