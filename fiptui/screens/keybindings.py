"""Редактор горячих клавиш FIPT."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from ..i18n import tr
from ..settings import (
    KEYBINDING_DEFAULTS, RESERVED_KEYS, effective_keys, find_conflict, split_keys)

# (действие, ключ перевода)
ACTIONS = [
    ("quit", "kb_act_quit"), ("open_library", "kb_act_open_library"), ("toggle_pause", "kb_act_toggle_pause"),
    ("next", "kb_act_next"), ("prev", "kb_act_prev"), ("tracks", "kb_act_tracks"),
    ("toggle_queue", "kb_act_toggle_queue"), ("settings", "kb_act_settings"), ("open_url", "kb_act_open_url"),
    ("seek(-1)", "kb_act_seek_back"), ("seek(1)", "kb_act_seek_fwd"),
    ("volume(1)", "kb_act_vol_up"), ("volume(-1)", "kb_act_vol_down"), ("mute", "kb_act_mute"),
    ("cycle_repeat", "kb_act_cycle_repeat"), ("shuffle", "kb_act_shuffle"),
    ("speed(-1)", "kb_act_speed_down"), ("speed(1)", "kb_act_speed_up"), ("speed(0)", "kb_act_speed_reset"),
    ("remove", "kb_act_remove"), ("move(-1)", "kb_act_move_up"),
    ("move(1)", "kb_act_move_down"), ("clear_queue", "kb_act_clear_queue"),
    ("play_terminal", "kb_act_play_terminal"), ("cycle_layout", "kb_act_cycle_layout"),
]
assert {a for a, _ in ACTIONS} == set(KEYBINDING_DEFAULTS), "ACTIONS и KEYBINDING_DEFAULTS разошлись"

# Виджеты получают id по номеру строки. Нельзя называть методы экрана `_id`, `_classes` и т.п.:
# у виджетов Textual это служебные атрибуты (в `_id` лежит id), и такой метод затирается значением None.
INPUT_IDS = {action: f"kb_input_{i}" for i, (action, _name) in enumerate(ACTIONS)}


def display_value(action: str, saved: dict) -> str:
    """Что показать в поле: все клавиши действия через запятую (вместе с доп. клавишами по умолчанию)."""
    return ",".join(effective_keys(action, saved))


class KeybindingsScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    KeybindingsScreen { align: center middle; background: rgba(0,0,0,0.65); }
    #kb_box { width: 76; max-width: 98%; height: 92%; background: $panel; border: thick $primary; padding: 1 2; }
    #kb_scroll { height: 1fr; }
    .kb_row { height: 3; }
    .kb_name { width: 2fr; content-align: left middle; }
    .kb_input { width: 1fr; }
    #kb_error { height: auto; color: $error; }
    #kb_buttons { height: 3; margin-top: 1; }
    """

    def __init__(self, lang: str, settings: dict):
        super().__init__()
        self.lang = lang
        self.saved = dict(settings.get("keybindings") or {})

    def _t(self, key: str, **kw) -> str:
        return tr(self.lang, key, **kw)

    def _action_label(self, key: str) -> str:
        return self._t(key)

    def compose(self) -> ComposeResult:
        with Vertical(id="kb_box"):
            yield Label(self._t("kb_title"), markup=False)
            yield Label(self._t("kb_hint"), markup=False)
            with VerticalScroll(id="kb_scroll"):
                for action, name in ACTIONS:
                    with Horizontal(classes="kb_row"):
                        yield Label(self._action_label(name), classes="kb_name", markup=False)
                        yield Input(value=display_value(action, self.saved), placeholder=KEYBINDING_DEFAULTS[action],
                                    id=INPUT_IDS[action], classes="kb_input")
            yield Label("", id="kb_error", markup=False)     # в тексте ошибки бывают «[» и «]»
            with Horizontal(id="kb_buttons"):
                yield Button(self._t("kb_btn_reset"), id="kb_reset")
                yield Button(self._t("kb_btn_save"), variant="success", id="kb_save")
                yield Button(self._t("kb_btn_cancel"), id="kb_cancel")

    def _show_error(self, text: str) -> None:
        self.query_one("#kb_error", Label).update(text)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "kb_cancel":
            self.dismiss(None)
        elif event.button.id == "kb_reset":
            for action, _name in ACTIONS:
                self.query_one("#" + INPUT_IDS[action], Input).value = display_value(action, {})
            self._show_error("")
        elif event.button.id == "kb_save":
            self._save()

    def _save(self) -> None:
        values = {action: self.query_one("#" + INPUT_IDS[action], Input).value for action, _name in ACTIONS}
        result, error = build_keybindings(values, self._label_for, self.lang)
        if result is None:
            self._show_error(error)
            return
        self.dismiss({"keybindings": result})

    def _label_for(self, action: str) -> str:
        return self._action_label(dict(ACTIONS)[action])


def build_keybindings(values: dict[str, str], label_for, lang: str) -> tuple[dict | None, str]:
    """Проверяет введённые значения. -> (словарь для настроек, '') либо (None, текст ошибки)."""
    result: dict[str, str] = {}
    for action, text in values.items():
        text = (text or "").strip()
        if not text:
            keys = effective_keys(action, {})                      # пустое поле = по умолчанию
        else:
            keys = split_keys(text)
            if not keys:
                return None, tr(lang, "kb_err_invalid", action=label_for(action), keyname=text)
        reserved = next((k for k in keys if k in RESERVED_KEYS), None)
        if reserved:
            return None, tr(lang, "kb_err_reserved", keyname=reserved, action=label_for(action))
        # значения по умолчанию храним как есть: тогда доп. клавиши (q, o, s, =) продолжают работать
        result[action] = KEYBINDING_DEFAULTS[action] if keys == effective_keys(action, {}) else ",".join(keys)
    conflict = find_conflict(result)
    if conflict:
        key, first, second = conflict
        return None, tr(lang, "kb_err_conflict", keyname=key, a=label_for(first), b=label_for(second))
    return result, ""

