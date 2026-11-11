"""Выбор звуковой дорожки и субтитров текущего файла.

Результат: {"aid": "no"|int, "sid": "no"|int} или None.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select

from ..i18n import tr
from ..mpv import describe_track, tracks_of


def _options(tracks: list[dict], off_label: str) -> tuple[list[tuple[str, str]], str]:
    opts = [(off_label, "no")]
    current = "no"
    for t in tracks:
        opts.append((describe_track(t), str(t.get("id"))))
        if t.get("selected"):
            current = str(t.get("id"))
    return opts, current


def _to_mpv(value) -> str | int:
    return int(value) if isinstance(value, str) and value.isdigit() else "no"


class TracksScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    TracksScreen { align: center middle; background: rgba(0, 0, 0, 0.6); }
    #tr_container { width: 64; max-width: 98%; height: auto; max-height: 92%; background: $panel;
                    border: thick $primary; padding: 1 2; }
    .tr_label { margin-top: 1; }
    #tr_buttons { height: 3; margin-top: 1; }
    .tr_btn { margin-right: 1; }
    """

    def __init__(self, lang: str, track_list):
        super().__init__()
        self._lang = lang
        self._audio = tracks_of(track_list, "audio")
        self._subs = tracks_of(track_list, "sub")

    def _t(self, key: str, **kw) -> str:
        return tr(self._lang, key, **kw)

    def compose(self) -> ComposeResult:
        a_opts, a_cur = _options(self._audio, self._t("tracks_off"))
        s_opts, s_cur = _options(self._subs, self._t("tracks_off"))
        with Vertical(id="tr_container"):
            yield Label(self._t("tracks_title"))
            if not self._audio and not self._subs:
                yield Label(self._t("tracks_none"), classes="tr_label")
            else:
                yield Label(self._t("tracks_audio"), classes="tr_label")
                yield Select(a_opts, value=a_cur, allow_blank=False, id="sel_aid")
                yield Label(self._t("tracks_sub"), classes="tr_label")
                yield Select(s_opts, value=s_cur, allow_blank=False, id="sel_sid")
            with Horizontal(id="tr_buttons"):
                if self._audio or self._subs:
                    yield Button(self._t("btn_apply"), variant="success", id="tr_ok", classes="tr_btn")
                yield Button(self._t("btn_cancel"), id="tr_cancel", classes="tr_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tr_cancel":
            self.dismiss(None)
        elif event.button.id == "tr_ok":
            self.dismiss({
                "aid": _to_mpv(self.query_one("#sel_aid", Select).value),
                "sid": _to_mpv(self.query_one("#sel_sid", Select).value),
            })

    def action_cancel(self) -> None:
        self.dismiss(None)
