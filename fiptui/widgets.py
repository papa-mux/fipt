"""Мелкие виджеты плеера: кнопка без фокуса, кликабельная полоса перемотки."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button

from .media import bar_cells, click_fraction
from .themes import rich_color


class FlatButton(Button, can_focus=False):
    """Кнопка, не забирающая фокус: пробел/стрелки остаются у очереди и горячих клавиш."""


class SeekBar(Widget):
    """Полоса прогресса. Клик по ней перематывает трек."""

    DEFAULT_CSS = """
    SeekBar { height: 1; width: 1fr; }
    """

    class Seek(Message):
        """Пользователь кликнул по полосе: fraction — доля длительности (0..1)."""

        def __init__(self, fraction: float) -> None:
            super().__init__()
            self.fraction = fraction

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pos = None
        self._dur = None

    def set_progress(self, position, duration) -> None:
        if (position, duration) != (self._pos, self._dur):
            self._pos, self._dur = position, duration
            self.refresh()

    def _colors(self) -> tuple[str, str]:
        try:
            css = self.app.get_css_variables()
        except Exception:  # noqa: BLE001
            css = {}
        hot = rich_color(css.get("primary"), "#5ea1ff")
        cold = rich_color(css.get("foreground"), "#a0a0a0")   # «пустая» часть — приглушённый текст
        return hot, cold

    def render(self) -> Text:
        width = self.size.width
        if width <= 0:
            return Text("")
        filled = bar_cells(width, self._pos, self._dur)
        try:
            hot, cold = self._colors()
            text = Text(no_wrap=True, overflow="crop")
            text.append("━" * max(0, filled - 1), style=hot)
            if filled > 0:
                text.append("●", style=hot)
            text.append("─" * (width - filled), style=f"dim {cold}")
            return text
        except Exception:  # noqa: BLE001  — сбой стиля не должен ронять всё приложение
            return Text("━" * max(0, filled - 1) + ("●" if filled else "") + "─" * (width - filled), no_wrap=True)

    def on_click(self, event: events.Click) -> None:
        if self._dur:
            self.post_message(self.Seek(click_fraction(event.x, self.size.width)))
