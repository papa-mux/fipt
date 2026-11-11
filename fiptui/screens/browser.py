"""Библиотека: обзор папок с музыкой/видео (аналог файлового меню PascalTUI).

Результат экрана: (действие, путь, текущая_папка) или None.
Действия: "play" (файл или папка), "add" (файл или папка), "url".
"""
from __future__ import annotations

import os

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView

from ..i18n import tr
from ..media import list_dir


class EntryItem(ListItem):
    def __init__(self, name: str, is_dir: bool):
        icon = "📁" if is_dir else "🎵"
        super().__init__(Label(Text(f"{icon} {name}")))
        self.entry_name = name
        self.is_dir = is_dir


class BrowserScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    CSS = """
    BrowserScreen { align: center middle; background: rgba(0, 0, 0, 0.5); }
    #lib_container { width: 64; max-width: 98%; height: 92%; background: $panel; border: thick $primary; padding: 1; }
    #lib_path { height: 1; margin-bottom: 1; color: $text-muted; }
    #lib_search { margin-bottom: 1; }
    #lib_list { background: $surface; border: solid $primary; height: 1fr; margin-bottom: 1; }
    .lib_row { height: 3; margin-bottom: 1; }
    .lib_btn { width: 1fr; margin-right: 1; }
    """

    def __init__(self, start_dir: str, lang: str):
        super().__init__()
        self._lang = lang
        self._dir = start_dir if os.path.isdir(start_dir) else os.path.expanduser("~")
        self._query = ""

    def _t(self, key: str, **kw) -> str:
        return tr(self._lang, key, **kw)

    def compose(self) -> ComposeResult:
        with Vertical(id="lib_container"):
            yield Label(self._t("lib_title"))
            yield Label("", id="lib_path", markup=False)
            yield Input(placeholder=self._t("lib_search_ph"), id="lib_search")
            yield ListView(id="lib_list")
            with Horizontal(classes="lib_row"):
                yield Button(self._t("lib_up"), id="lib_up", classes="lib_btn")
                yield Button(self._t("lib_home"), id="lib_home", classes="lib_btn")
                yield Button(self._t("lib_url"), id="lib_url", classes="lib_btn")
            with Horizontal(classes="lib_row"):
                yield Button(self._t("lib_play"), variant="success", id="lib_play", classes="lib_btn")
                yield Button(self._t("lib_add"), variant="primary", id="lib_add", classes="lib_btn")
            yield Button(self._t("lib_close"), id="lib_close", classes="lib_btn")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#lib_list", ListView).focus()

    # ---- список ----
    def _rebuild(self) -> None:
        self.query_one("#lib_path", Label).update(Text(self._dir))
        entries = list_dir(self._dir)
        if self._query:
            entries = [e for e in entries if self._query in e[0].lower()]
        lv = self.query_one("#lib_list", ListView)
        lv.clear()
        for name, is_dir in entries:
            lv.append(EntryItem(name, is_dir))
        if entries:
            lv.index = 0

    def _highlighted(self) -> EntryItem | None:
        child = self.query_one("#lib_list", ListView).highlighted_child
        return child if isinstance(child, EntryItem) else None

    def _go(self, directory: str) -> None:
        if os.path.isdir(directory):
            self._dir = directory
            self._query = ""
            self.query_one("#lib_search", Input).value = ""
            self._rebuild()

    def _full(self, name: str) -> str:
        return os.path.join(self._dir, name)

    def _finish(self, action: str, path: str) -> None:
        self.dismiss((action, path, self._dir))

    # ---- события ----
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "lib_search":
            self._query = event.value.lower().strip()
            self._rebuild()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        item = event.item
        if not isinstance(item, EntryItem):
            return
        if item.is_dir:
            self._go(self._full(item.entry_name))
        else:
            self._finish("play", self._full(item.entry_name))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        item = self._highlighted()
        if bid == "lib_close":
            self.dismiss(None)
        elif bid == "lib_up":
            parent = os.path.dirname(self._dir.rstrip(os.sep)) or os.sep
            self._go(parent)
        elif bid == "lib_home":
            self._go(os.path.expanduser("~"))
        elif bid == "lib_url":
            self._finish("url", "")
        elif bid == "lib_play":
            # выбран элемент — играем его; иначе — всю текущую папку
            self._finish("play", self._full(item.entry_name) if item else self._dir)
        elif bid == "lib_add":
            self._finish("add", self._full(item.entry_name) if item else self._dir)

    def action_close(self) -> None:
        self.dismiss(None)
