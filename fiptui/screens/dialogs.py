"""Простые модальные диалоги: ввод текста и подтверждение."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class InputDialog(ModalScreen):
    """Диалог с полем ввода.

    Результат: строка (может быть пустой, если allow_empty=True) или None при отмене.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    InputDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #dialog_container {
        width: 60;
        max-width: 95%;
        height: auto;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }
    #dialog_input {
        margin-top: 1;
        margin-bottom: 1;
    }
    #dialog_buttons {
        height: 3;
    }
    .dialog_btn {
        margin-right: 1;
    }
    """

    def __init__(self, title: str, placeholder: str = "", default: str = "",
                 ok_label: str = "OK", cancel_label: str = "Cancel", allow_empty: bool = False):
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._default = default
        self._ok_label = ok_label
        self._cancel_label = cancel_label
        self._allow_empty = allow_empty

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog_container"):
            yield Label(self._title, markup=False)
            yield Input(value=self._default, placeholder=self._placeholder, id="dialog_input")
            with Horizontal(id="dialog_buttons"):
                yield Button(self._ok_label, variant="success", id="dialog_ok", classes="dialog_btn")
                yield Button(self._cancel_label, variant="default", id="dialog_cancel", classes="dialog_btn")

    def on_mount(self) -> None:
        self.query_one("#dialog_input", Input).focus()

    def _submit(self, value: str) -> None:
        value = value.strip()
        self.dismiss(value if (value or self._allow_empty) else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog_ok":
            self._submit(self.query_one("#dialog_input", Input).value)
        elif event.button.id == "dialog_cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen):
    """Подтверждение (да/нет). Результат: True / False."""

    BINDINGS = [Binding("escape", "no", "No", show=False)]

    CSS = """
    ConfirmDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #confirm_container {
        width: 56;
        max-width: 95%;
        height: auto;
        background: $panel;
        border: thick $error;
        padding: 1 2;
    }
    #confirm_buttons {
        height: 3;
        margin-top: 1;
    }
    .confirm_btn {
        margin-right: 1;
    }
    """

    def __init__(self, message: str, yes_label: str = "Yes", no_label: str = "No"):
        super().__init__()
        self._message = message
        self._yes = yes_label
        self._no = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_container"):
            yield Label(self._message, markup=False)
            with Horizontal(id="confirm_buttons"):
                yield Button(self._yes, variant="error", id="confirm_yes", classes="confirm_btn")
                yield Button(self._no, variant="default", id="confirm_no", classes="confirm_btn")

    def on_mount(self) -> None:
        self.query_one("#confirm_no", Button).focus()  # по умолчанию «Нет» — защита от случайного Enter

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm_yes")

    def action_no(self) -> None:
        self.dismiss(False)
