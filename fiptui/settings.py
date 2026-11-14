"""Настройки: значения по умолчанию лежат в data/defaults.json."""
from __future__ import annotations

import re

from . import paths
from .jsonio import read_json, write_json_atomic

_MINIMAL = {
    "language": "ru",
    "theme": "fipt-dark",
    "mpv_path": "mpv",
    "video": "window",
    "icons": "braille",
    "seekbar": "line",
    "queue_border": "round",
    "show_log": False,
    "xf86_media_keys": True,
    "mpris": True,
    "keybindings": {},
}

KEYBINDING_DEFAULTS = {
    "quit": "ctrl+q", "open_library": "ctrl+o", "toggle_pause": "space",
    "next": "n", "prev": "p", "tracks": "t", "toggle_queue": "l",
    "settings": "f2", "open_url": "u", "seek(-1)": "left", "seek(1)": "right",
    "volume(1)": "plus", "volume(-1)": "minus", "mute": "m",
    "cycle_repeat": "r", "shuffle": "x", "speed(-1)": "left_square_bracket",
    "speed(1)": "right_square_bracket", "speed(0)": "backslash",
    "remove": "delete", "move(-1)": "shift+up", "move(1)": "shift+down",
    "clear_queue": "c", "play_terminal": "v", "cycle_layout": "f3",
}

# Доп. клавиши действий: работают, пока действие не переназначено; после переназначения остаётся выбор пользователя.
ALIAS_KEYS = {
    "quit": ["q"], "open_library": ["o"], "settings": ["s"], "volume(1)": ["equals_sign"],
    "move(-1)": ["K"], "move(1)": ["J"],
}

# Клавиши, которые занимает сам Textual / терминал: назначать их нельзя (привязка просто не сработает).
RESERVED_KEYS = {"ctrl+p": "палитра команд", "ctrl+c": "выход из Textual", "escape": "закрытие окон",
                 "tab": "переход фокуса", "shift+tab": "переход фокуса"}

MODIFIERS = ("ctrl", "shift", "alt", "meta")
MAX_KEYS_PER_ACTION = 4

# Как называет Textual клавиши, которые удобнее вводить символом или коротким именем.
_KEY_NAMES = {
    "+": "plus", "-": "minus", "=": "equals_sign", "[": "left_square_bracket", "]": "right_square_bracket",
    "\\": "backslash", " ": "space", ",": "comma", ".": "full_stop", "/": "slash", ";": "semicolon",
    "'": "apostrophe", "`": "grave_accent", "esc": "escape", "return": "enter", "del": "delete",
    "pgup": "pageup", "pgdn": "pagedown", "ins": "insert", "bksp": "backspace",
}
_KEY_RE = re.compile(r"((?:[A-Za-z]+\+)*)(.+)")

INT_LIMITS = {
    "volume": (0, 130),
    "seek_step": (1, 120),
    "volume_step": (1, 25),
}

CHOICES = {
    "video": ("window", "off"),
    "replaygain": ("no", "track", "album"),
    "terminal_vo": ("tct", "kitty", "sixel"),
    "layout": ("mobile", "classic"),
    "icons": ("braille", "text"),
    "seekbar": ("line", "block", "dots", "thin"),
    "queue_border": ("round", "heavy", "double", "solid", "dashed", "none"),
}

# строки, которые могут быть пустыми
_ALLOW_EMPTY = {"last_dir", "mpv_args"}


def normalize_key(text: str) -> str:
    """Приводит клавишу к имени Textual ('Ctrl+P' -> 'ctrl+p', '+' -> 'plus'). Пустая строка — не клавиша."""
    text = (text or "").strip()
    match = _KEY_RE.fullmatch(text)
    if not match:
        return ""
    prefix, key = match.groups()
    mods = [m.lower() for m in prefix.split("+") if m]
    if any(m not in MODIFIERS for m in mods) or len(set(mods)) != len(mods):
        return ""
    if len(key) == 1:
        key = _KEY_NAMES.get(key, key)
        if len(key) == 1 and not key.isalnum():
            return ""
        if mods:
            key = key.lower()          # shift+K и shift+k — одно и то же; голая «K» остаётся заглавной
    else:
        key = _KEY_NAMES.get(key.lower(), key.lower())
        if not re.fullmatch(r"[a-z0-9_]+", key):
            return ""
    return "+".join(mods + [key])


def split_keys(text: str) -> list[str]:
    """'plus, equals_sign' -> ['plus', 'equals_sign']. Пустой список, если хоть одна клавиша некорректна."""
    text = (text or "").strip()
    if text == ",":
        return ["comma"]
    keys: list[str] = []
    for part in text.split(","):
        if not part.strip():
            continue
        key = normalize_key(part)
        if not key:
            return []
        if key not in keys:
            keys.append(key)
    return keys[:MAX_KEYS_PER_ACTION] if len(keys) <= MAX_KEYS_PER_ACTION else []


def effective_keys(action: str, overrides: dict | None) -> list[str]:
    """Клавиши действия: выбор пользователя (одна или несколько) либо значение по умолчанию + доп. клавиши."""
    default = KEYBINDING_DEFAULTS[action]
    chosen = (overrides or {}).get(action)
    if isinstance(chosen, str) and chosen.strip() and chosen.strip() != default:
        keys = split_keys(chosen)
        if keys:
            return keys
    return [default] + ALIAS_KEYS.get(action, [])


def find_conflict(overrides: dict) -> tuple[str, str, str] | None:
    """(клавиша, действие 1, действие 2), если одна клавиша назначена двум действиям; иначе None."""
    used: dict[str, str] = {}
    for action in KEYBINDING_DEFAULTS:
        for key in effective_keys(action, overrides):
            if key in used and used[key] != action:
                return key, used[key], action
            used[key] = action
    return None


def _defaults_file() -> dict:
    data = read_json(paths.DATA_DIR / "defaults.json", {})
    return data if isinstance(data, dict) else {}


def default_settings() -> dict:
    result = dict(_MINIMAL)
    raw = _defaults_file().get("settings", {})
    if isinstance(raw, dict):
        result.update(raw)
    result.setdefault("music_dir", paths.default_music_dir())
    result.setdefault("last_dir", "")
    result["keybindings"] = {**KEYBINDING_DEFAULTS, **(result.get("keybindings") if isinstance(result.get("keybindings"), dict) else {})}
    return result


def sanitize(raw: dict, base: dict) -> dict:
    """Оставляет только известные ключи и приводит значения к типу значения по умолчанию."""
    out = dict(base)
    if not isinstance(raw, dict):
        return out
    for key, default in base.items():
        if key not in raw:
            continue
        value = raw[key]
        if key == "keybindings" and isinstance(value, dict):
            allowed = {}
            for action, fallback in KEYBINDING_DEFAULTS.items():
                keys = split_keys(value.get(action)) if isinstance(value.get(action), str) else []
                allowed[action] = ",".join(keys) if keys and not set(keys) & set(RESERVED_KEYS) else fallback
            # конфликт (файл правили руками) — возвращаем все клавиши по умолчанию, а не ломаем управление
            out[key] = allowed if find_conflict(allowed) is None else dict(KEYBINDING_DEFAULTS)
        elif isinstance(default, dict):
            continue
        elif isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool):
                lo, hi = INT_LIMITS.get(key, (None, None))
                if lo is not None:
                    value = max(lo, min(hi, value))
                out[key] = value
        elif isinstance(default, str):
            if isinstance(value, str) and (value.strip() or key in _ALLOW_EMPTY):
                if key in CHOICES and value not in CHOICES[key]:
                    continue
                out[key] = value
    return out


def load_settings() -> dict:
    raw = read_json(paths.SETTINGS_PATH, {})
    return sanitize(raw, default_settings())


def save_settings(settings: dict) -> str | None:
    """Возвращает текст ошибки или None."""
    return write_json_atomic(paths.SETTINGS_PATH, settings)
