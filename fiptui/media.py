"""Медиатека: просмотр папок, сортировка, форматирование времени (без UI)."""
from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlparse

AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma",
    ".ape", ".wv", ".mka", ".aiff", ".aif", ".alac", ".mpc", ".tta",
}
VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv",
}
PLAYLIST_EXTS = {".m3u", ".m3u8", ".pls"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS | PLAYLIST_EXTS

_NUM_RE = re.compile(r"(\d+)")


def natural_key(name: str) -> list:
    """'track2' < 'track10'. Регистр не важен."""
    return [int(p) if p.isdigit() else p.lower() for p in _NUM_RE.split(name)]


def is_url(text: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", (text or "").strip()))


def is_media(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


def is_playlist(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in PLAYLIST_EXTS


def list_dir(dir_path: str) -> list[tuple[str, bool]]:
    """[(имя, is_dir)] — папки и медиафайлы, сначала папки. Скрытые пропускаются."""
    if not os.path.isdir(dir_path):
        return []
    items: list[tuple[str, bool]] = []
    try:
        names = os.listdir(dir_path)
    except OSError:
        return []
    for f in names:
        if f.startswith("."):
            continue
        full = os.path.join(dir_path, f)
        if os.path.isdir(full):
            items.append((f, True))
        elif is_media(f):
            items.append((f, False))
    return sorted(items, key=lambda x: (not x[1], natural_key(x[0])))


def folder_media(dir_path: str, recursive: bool = False) -> list[str]:
    """Абсолютные пути медиафайлов папки в «человеческом» порядке."""
    if not os.path.isdir(dir_path):
        return []
    result: list[str] = []
    if not recursive:
        for name, is_dir in list_dir(dir_path):
            if not is_dir and not is_playlist(name):
                result.append(os.path.join(dir_path, name))
        return result
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=natural_key)
        for f in sorted(files, key=natural_key):
            if not f.startswith(".") and is_media(f) and not is_playlist(f):
                result.append(os.path.join(root, f))
    return result


def display_name(entry: dict) -> str:
    """Название элемента очереди: title, иначе имя файла / хвост URL."""
    title = entry.get("title")
    if isinstance(title, str) and title.strip():
        return fix_mojibake(title.strip())
    return short_name(str(entry.get("filename", "")))


def short_name(path_or_url: str) -> str:
    if is_url(path_or_url):
        parsed = urlparse(path_or_url)
        tail = unquote(os.path.basename(parsed.path.rstrip("/")))
        return tail or parsed.netloc or path_or_url
    return os.path.basename(path_or_url) or path_or_url


def format_time(seconds) -> str:
    """73 -> '1:13', 3700 -> '1:01:40'; None/мусор -> '--:--'."""
    if not isinstance(seconds, (int, float)) or seconds != seconds or seconds < 0:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fix_mojibake(text: str) -> str:
    """Чинит «кракозябры» в тегах: ID3-тег записан в UTF-8 или cp1251, но помечен как Latin-1.

    'Ð§ÑÐ¶Ð¾Ð¹' -> 'Чужой', 'Èíîõîäåö' -> 'Иноходец'. Нормальный текст (в том числе настоящая
    кириллица, ascii, «Café») возвращается без изменений.
    """
    if not isinstance(text, str) or not text or text.isascii():
        return text
    for codec in ("latin-1", "cp1252"):
        try:
            raw = text.encode(codec)
        except UnicodeEncodeError:
            continue                      # есть символы вне кодировки — значит, это уже настоящий Unicode
        try:
            return raw.decode("utf-8")    # самый частый случай: UTF-8, прочитанный как Latin-1
        except UnicodeDecodeError:
            pass
        try:
            fixed = raw.decode("cp1251")  # старые русские теги
        except UnicodeDecodeError:
            continue
        letters = [c for c in fixed if c.isalpha()]
        cyr = sum("\u0400" <= c <= "\u04ff" for c in letters)
        if letters and cyr / len(letters) >= 0.7:   # «Étienne» -> «Йtienne» не пройдёт
            return fixed
    return text


def meta_get(metadata, *keys: str) -> str:
    """Регистронезависимый поиск тега (ARTIST / artist / Artist)."""
    if not isinstance(metadata, dict):
        return ""
    lowered = {str(k).lower(): v for k, v in metadata.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str) and value.strip():
            return fix_mojibake(value.strip())
    return ""


def split_now_playing(title: str, metadata) -> tuple[str, str, str]:
    """-> (название, исполнитель, альбом). Название: тег title, иначе media-title от mpv."""
    artist = meta_get(metadata, "artist", "album_artist", "performer")
    album = meta_get(metadata, "album")
    main = meta_get(metadata, "title") or fix_mojibake(title or "")
    return main, artist, album


def now_playing_text(title: str, metadata) -> tuple[str, str]:
    """-> (строка заголовка «Исполнитель — Название», альбом) для классической панели."""
    main, artist, album = split_now_playing(title, metadata)
    if artist and main:
        main = f"{artist} — {main}"
    return main, album


def bar_cells(width: int, position, duration) -> int:
    """Сколько клеток полосы перемотки закрашено (0..width). Для потоков без длительности — 0."""
    if width <= 0 or not isinstance(position, (int, float)) or not isinstance(duration, (int, float)):
        return 0
    if duration <= 0 or position != position or duration != duration:
        return 0
    return max(0, min(width, int(round(width * position / duration))))


def click_fraction(x: int, width: int) -> float:
    """Клик по полосе (клетка x из width) -> доля 0..1 для seek."""
    if width <= 1:
        return 0.0
    return max(0.0, min(1.0, x / (width - 1)))
