"""Мелкие виджеты плеера: кнопка без фокуса, кликабельная полоса перемотки."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button

from .media import bar_cells, click_fraction
from .themes import rich_color


def theme_color(widget, key: str, fallback: str) -> str:
    """Цвет из переменных темы Textual в виде '#rrggbb' (безопасно для Rich)."""
    try:
        return rich_color(widget.app.get_css_variables().get(key), fallback)
    except Exception:  # noqa: BLE001
        return fallback


# Стили полосы перемотки: (заполнено, «бегунок» в конце заполненной части, пусто)
SEEK_STYLES = {
    "line": ("━", "●", "─"),
    "block": ("█", "", "░"),
    "dots": ("●", "", "·"),
    "thin": ("▬", "", "▭"),
}


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
        hot = theme_color(self, "fipt-seek", theme_color(self, "primary", "#5ea1ff"))
        cold = theme_color(self, "foreground", "#a0a0a0")   # «пустая» часть — приглушённый текст
        return hot, cold

    def render(self) -> Text:
        width = self.size.width
        if width <= 0:
            return Text("")
        filled = bar_cells(width, self._pos, self._dur)
        full, head, empty = SEEK_STYLES.get(self._style(), SEEK_STYLES["line"])
        try:
            hot, cold = self._colors()
            text = Text(no_wrap=True, overflow="crop")
            text.append(full * max(0, filled - (1 if head else 0)), style=hot)
            if filled > 0 and head:
                text.append(head, style=hot)
            text.append(empty * (width - filled), style=f"dim {cold}")
            return text
        except Exception:  # noqa: BLE001  — сбой стиля не должен ронять всё приложение
            return Text(full * max(0, filled - 1) + (head if filled else "") + empty * (width - filled), no_wrap=True)

    def _style(self) -> str:
        try:
            return str(self.app.settings.get("seekbar", "line"))
        except Exception:  # noqa: BLE001
            return "line"

    def on_click(self, event: events.Click) -> None:
        if self._dur:
            self.post_message(self.Seek(click_fraction(event.x, self.size.width)))


class IconButton(Widget, can_focus=False):
    """Кнопка-иконка (Брайль-графика или символ). Клик -> IconButton.Pressed(action).

    state: 'normal' — обычная, 'active' — выделена цветом темы (режим включён), 'dim' — приглушена.
    """

    DEFAULT_CSS = """
    IconButton { width: 1fr; height: auto; min-height: 3; content-align: center middle; }
    IconButton:hover { background: $boost; }
    """

    class Pressed(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, action: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.action = action
        self._lines: tuple[str, ...] = ()
        self._state = "normal"

    def set_icon(self, lines, state: str = "normal") -> None:
        lines = tuple(lines)
        if (lines, state) != (self._lines, self._state):
            self._lines, self._state = lines, state
            self.refresh(layout=True)

    def render(self) -> Text:
        if self._state == "active":
            style = theme_color(self, "fipt-active", theme_color(self, "primary", "#5ea1ff"))
        elif self._state == "dim":
            style = "dim " + theme_color(self, "foreground", "#a0a0a0")
        else:
            style = theme_color(self, "foreground", "#d0d0d0")
        return Text("\n".join(self._lines), style=style, justify="center", no_wrap=True)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Pressed(self.action))
