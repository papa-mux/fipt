"""Пути: автоопределение окружения (обычный Linux / macOS / Termux)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_DIR_NAME = "fipt"

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent  # папка, где лежит tui

TERMUX_PREFIX = os.environ.get("PREFIX", "")
IS_TERMUX = bool(TERMUX_PREFIX) and "com.termux" in TERMUX_PREFIX


def candidate_share_dirs() -> list[str]:
    """Возможные места, где могут лежать locales/ themes/ data/."""
    candidates: list[str] = []
    if IS_TERMUX:
        candidates.append(os.path.join(TERMUX_PREFIX, "share", APP_DIR_NAME))
    candidates += [
        "/usr/local/share/" + APP_DIR_NAME,
        "/usr/share/" + APP_DIR_NAME,
        os.path.join(os.path.expanduser("~"), ".local", "share", APP_DIR_NAME),
        str(ROOT_DIR),  # «переносная» версия рядом со скриптом
    ]
    return candidates


def find_resource_dir() -> Path:
    """Где лежат locales/, themes/, data/ (рядом с кодом или в share)."""
    for d in [ROOT_DIR] + [Path(x) for x in candidate_share_dirs()]:
        if (d / "locales").is_dir():
            return d
    return ROOT_DIR


def default_music_dir() -> str:
    """Папка с музыкой по умолчанию: Termux-хранилище, ~/Music или домашняя."""
    home = Path.home()
    options = []
    if IS_TERMUX:
        options += [home / "storage" / "music", home / "storage" / "shared" / "Music"]
    options += [home / "Music", home / "Музыка"]
    for p in options:
        if p.is_dir():
            return str(p)
    return str(home)


LEGACY_DIR_NAME = "mpv-tui"   # прежнее имя проекта


def _config_dir() -> Path:
    base = Path.home() / ".config"
    path = base / APP_DIR_NAME
    legacy = base / LEGACY_DIR_NAME
    try:
        if not path.exists() and legacy.is_dir():
            shutil.copytree(legacy, path)      # одноразовый перенос настроек, очереди и своих тем
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


CONFIG_DIR = _config_dir()
SETTINGS_PATH = CONFIG_DIR / "settings.json"
QUEUE_PATH = CONFIG_DIR / "queue.json"

# Пользовательские расширения (кладите свои файлы сюда)
USER_THEMES_DIR = CONFIG_DIR / "themes"
USER_LOCALES_DIR = CONFIG_DIR / "locales"

RES_DIR = find_resource_dir()
DATA_DIR = RES_DIR / "data"
LOCALES_DIR = RES_DIR / "locales"
THEMES_DIR = RES_DIR / "themes"
