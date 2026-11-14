"""Темы: встроенные лежат в themes/*.json, пользовательские — в ~/.config/abc-pascal-tui/themes."""
from __future__ import annotations

import re

from . import paths
from .jsonio import read_json, write_json_atomic

COLOR_KEYS = ("primary", "secondary", "accent", "background", "surface", "panel", "foreground")
OPTIONAL_COLOR_KEYS = ("warning", "error", "success")
# Цвета самого fipt (не часть темы Textual): полоса перемотки и «включённые» иконки. По умолчанию — primary.
EXTRA_COLOR_KEYS = ("seek", "active")
EDITOR_KEYS = COLOR_KEYS + EXTRA_COLOR_KEYS

_HEX_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-")
    return slug or "theme"


def normalize_hex(value) -> str | None:
    """'#abc' / 'AABBCC' -> '#aabbcc'; мусор -> None."""
    if value is None:
        return None
    text = str(value).strip()
    m = _HEX_RE.fullmatch(text)
    if not m:
        return None
    digits = m.group(1).lower()
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits


def validate_theme(data) -> dict | None:
    """Возвращает очищенную тему или None, если она непригодна."""
    if not isinstance(data, dict):
        return None
    name = str(data.get("name", "")).strip()
    primary = normalize_hex(data.get("primary"))
    if not name or not primary:
        return None
    out = {
        "name": name,
        "dark": bool(data.get("dark", True)),
    }
    for key in COLOR_KEYS + OPTIONAL_COLOR_KEYS + EXTRA_COLOR_KEYS:
        value = normalize_hex(data.get(key))
        if value:
            out[key] = value
    return out


def _load_dir(directory, user: bool, result: dict[str, dict]) -> None:
    if not directory.is_dir():
        return
    for file in sorted(directory.glob("*.json")):
        theme = validate_theme(read_json(file))
        if theme:
            theme["_user"] = user
            result[theme["name"]] = theme


def load_all() -> dict[str, dict]:
    """name -> тема. Пользовательские перекрывают встроенные с тем же именем."""
    result: dict[str, dict] = {}
    _load_dir(paths.THEMES_DIR, False, result)
    _load_dir(paths.USER_THEMES_DIR, True, result)
    return result


def save_user_theme(theme: dict) -> str | None:
    """Сохраняет тему в пользовательскую папку. Возвращает ошибку или None."""
    clean = validate_theme(theme)
    if not clean:
        return "invalid theme"
    return write_json_atomic(paths.USER_THEMES_DIR / (slugify(clean["name"]) + ".json"), clean)


def delete_user_theme(name: str) -> bool:
    directory = paths.USER_THEMES_DIR
    if not directory.is_dir():
        return False
    for file in directory.glob("*.json"):
        theme = validate_theme(read_json(file))
        if theme and theme["name"] == name:
            try:
                file.unlink()
                return True
            except OSError:
                return False
    return False


def to_textual_theme(data: dict):
    """dict -> textual.theme.Theme (или None, если Textual слишком старый)."""
    try:
        from textual.theme import Theme
    except Exception:  # noqa: BLE001
        return None
    kwargs = {k: data[k] for k in COLOR_KEYS + OPTIONAL_COLOR_KEYS if k in data}
    extra = {f"fipt-{k}": data[k] for k in EXTRA_COLOR_KEYS if k in data}
    for variables in (extra, None):               # старый Textual без Theme(variables=...) — без доп. цветов
        try:
            if variables is None:
                return Theme(name=data["name"], dark=data.get("dark", True), **kwargs)
            return Theme(name=data["name"], dark=data.get("dark", True), variables=variables, **kwargs)
        except Exception:  # noqa: BLE001
            continue
    return None


_RICH_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")


def rich_color(value, fallback: str) -> str:
    """Цвет, безопасный для Rich: '#rrggbb'.

    Textual отдаёт часть переменных как '#RRGGBBAA' (с прозрачностью), Rich такое не разбирает
    и падает с MissingStyle. Альфа-канал отбрасываем; непонятное значение -> fallback.
    """
    m = _RICH_HEX_RE.match(value.strip()) if isinstance(value, str) else None
    if m:
        return "#" + m.group(1).lower()
    short = normalize_hex(value) if isinstance(value, str) else None
    return short or fallback
