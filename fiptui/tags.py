"""Теги треков (исполнитель — название) для очереди. Без UI.

mpv отдаёт метаданные только того файла, что играет сейчас, поэтому для всей очереди теги читает
`mutagen` (необязательная зависимость: pip install mutagen). Без него очередь показывает имена файлов
и «доучивается» по мере проигрывания (см. learn_from_metadata).
"""
from __future__ import annotations

import os

from .media import fix_mojibake, is_url, meta_get

# Не читаем теги у огромных видеофайлов: дорого и бессмысленно для очереди
_MAX_BYTES = 2 * 1024 ** 3


def _mutagen():
    try:
        import mutagen  # type: ignore
        return mutagen
    except ImportError:
        return None


def available() -> bool:
    return _mutagen() is not None


def format_name(artist: str, title: str) -> str:
    """'Исполнитель — Название'; без названия имя не строим (вернём '' — будет имя файла)."""
    artist, title = (artist or "").strip(), (title or "").strip()
    if not title:
        return ""
    return f"{artist} — {title}" if artist else title


def _first(tags, key: str) -> str:
    try:
        value = tags.get(key)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return fix_mojibake(str(value).strip()) if value else ""


def read_name(path: str) -> str:
    """Имя трека из тегов или '' (нет mutagen / нет тегов / не файл / битый файл)."""
    mutagen = _mutagen()
    if mutagen is None or is_url(path) or not os.path.isfile(path):
        return ""
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return ""
        audio = mutagen.File(path, easy=True)
        tags = getattr(audio, "tags", None) if audio is not None else None
        if not tags:
            return ""
        artist = _first(tags, "artist") or _first(tags, "albumartist") or _first(tags, "performer")
        return format_name(artist, _first(tags, "title"))
    except Exception:  # noqa: BLE001  — битые теги не должны ломать очередь
        return ""


def read_many(paths: list[str]) -> dict[str, str]:
    """{путь: имя из тегов или ''}. Блокирующая — вызывать в потоке."""
    return {p: read_name(p) for p in paths}


def learn_from_metadata(metadata) -> str:
    """Имя трека из свойства mpv `metadata` (для играющего файла, без mutagen)."""
    return format_name(
        meta_get(metadata, "artist", "album_artist", "performer"),
        meta_get(metadata, "title") or "",
    )
