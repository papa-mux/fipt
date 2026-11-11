"""Редактор пользовательской темы: цвета в #RRGGBB, сохраняется в ~/.config/fipt/themes."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, Switch

from .. import themes
from ..i18n import tr


class ThemeEditorScreen(ModalScreen):
    """Результат: сохранённая тема (dict) или None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    ThemeEditorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    #te_container {
        width: 56;
        max-width: 98%;
        height: 92%;
        background: $panel;
        border: thick $accent;
        padding: 1 2;
    }
    #te_scroll {
        height: 1fr;
    }
    .te_label {
        margin-top: 1;
    }
    .te_row {
        height: 3;
    }
    .te_input {
        width: 1fr;
    }
    .te_swatch {
        width: 6;
        height: 3;
        margin-left: 1;
        border: solid $primary;
    }
    #te_error {
        color: $error;
        height: auto;
    }
    #te_buttons {
        height: 3;
        margin-top: 1;
    }
    .te_btn {
        margin-right: 1;
    }
    """

    def __init__(self, lang: str, base: dict | None, base_is_user: bool):
        super().__init__()
        self._lang = lang
        base = base or {}
        self._base = base
        name = str(base.get("name", ""))
        if name and not base_is_user:
            name += "-copy"  # встроенные темы не перезаписываем — делаем копию
        self._name = name

    def _t(self, key: str, **kw) -> str:
        return tr(self._lang, key, **kw)

    def compose(self) -> ComposeResult:
        base = self._base
        with Vertical(id="te_container"):
            yield Label(self._t("theme_editor_title"))
            with VerticalScroll(id="te_scroll"):
                yield Label(self._t("theme_name"), classes="te_label")
                yield Input(value=self._name, id="te_name")
                with Horizontal(classes="te_row"):
                    yield Switch(value=bool(base.get("dark", True)), id="te_dark")
                    yield Label(self._t("theme_dark"), classes="te_label")
                for key in themes.COLOR_KEYS:
                    yield Label(self._t(f"theme_color_{key}"), classes="te_label")
                    with Horizontal(classes="te_row"):
                        yield Input(value=str(base.get(key, "")), placeholder="#RRGGBB",
                                    id=f"te_{key}", classes="te_input")
                        yield Static("", id=f"sw_{key}", classes="te_swatch")
                yield Label(self._t("theme_hint"), classes="te_label")
            yield Label("", id="te_error")
            with Horizontal(id="te_buttons"):
                yield Button(self._t("btn_save"), variant="success", id="te_save", classes="te_btn")
                yield Button(self._t("btn_cancel"), variant="default", id="te_cancel", classes="te_btn")

    def on_mount(self) -> None:
        for key in themes.COLOR_KEYS:
            self._paint(key, self.query_one(f"#te_{key}", Input).value)

    def _paint(self, key: str, value: str) -> None:
        color = themes.normalize_hex(value)
        swatch = self.query_one(f"#sw_{key}", Static)
        try:
            swatch.styles.background = color if color else "transparent"
        except Exception:  # noqa: BLE001
            pass

    @on(Input.Changed)
    def _color_changed(self, event: Input.Changed) -> None:
        iid = event.input.id or ""
        if iid.startswith("te_") and iid[3:] in themes.COLOR_KEYS:
            self._paint(iid[3:], event.value)

    def _error(self, text: str) -> None:
        self.query_one("#te_error", Label).update(text)

    def _save(self) -> None:
        name = self.query_one("#te_name", Input).value.strip()
        if not name:
            self._error(self._t("theme_err_name"))
            return
        data: dict = {
            "name": name,
            "dark": self.query_one("#te_dark", Switch).value,
        }
        for key in themes.COLOR_KEYS:
            raw = self.query_one(f"#te_{key}", Input).value.strip()
            if not raw:
                if key == "primary":
                    self._error(self._t("theme_err_primary"))
                    return
                continue
            color = themes.normalize_hex(raw)
            if not color:
                self._error(self._t("theme_err_color", field=self._t(f"theme_color_{key}")))
                return
            data[key] = color
        err = themes.save_user_theme(data)
        if err:
            self._error(self._t("theme_err_write", err=err))
            return
        clean = themes.validate_theme(data) or data
        if not self.app.register_user_theme(clean):  # type: ignore[attr-defined]
            self._error(self._t("theme_unsupported"))
            return
        self.dismiss(clean)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "te_save":
            self._save()
        elif event.button.id == "te_cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
