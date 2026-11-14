"""Доступ к иконкам кнопок: Брайль-графика (из assets/icons/*.svg) или простые символы."""
from __future__ import annotations

from .icons import BRAILLE

# запасной вариант для шрифтов/терминалов без Брайля
TEXT = {"prev": "|◀", "back": "«", "play": "▶", "pause": "❚❚", "fwd": "»", "next": "▶|",
        "shuffle": "⇄", "repeat": "↻", "voldn": "−", "volup": "+"}

NAMES = tuple(BRAILLE)


def get_icon(name: str, style: str = "text", size: str = "s") -> tuple[str, ...]:
    """Строки иконки. size: 's' (6x3 клетки) или 'l' (8x4). Неизвестное имя -> пустая иконка."""
    if style == "braille":
        sizes = BRAILLE.get(name)
        if sizes:
            return sizes.get(size) or sizes["s"]
    glyph = TEXT.get(name)
    return (glyph,) if glyph else ()
