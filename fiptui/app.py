"""Главное приложение fipt: оболочка над mpv."""
from __future__ import annotations

import asyncio
import os

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Label, OptionList, RichLog
from textual.widgets.option_list import Option

from . import media, tags, themes
from .commands import SettingsCommands
from .i18n import tr
from .mpv import (
    NEXT_REPEAT,
    MpvClient,
    MpvError,
    describe_end_file,
    repeat_mode,
    repeat_properties,
    resolve_binary,
    run_in_terminal,
)
from .paths import SETTINGS_PATH
from .queue_store import load_queue, save_queue
from .screens import (
    BrowserScreen,
    ConfirmDialog,
    GeneralScreen,
    InputDialog,
    PathsScreen,
    PlaybackScreen,
    ThemesScreen,
    TracksScreen,
    KeybindingsScreen,
)
from .settings import (
    CHOICES, INT_LIMITS, KEYBINDING_DEFAULTS, effective_keys, load_settings, sanitize, save_settings)
from .iconset import get_icon
from .mpris import MprisService, PlayerView
from .widgets import FlatButton, IconButton, SeekBar
from .xf86 import XF86MediaKeys

DEFAULT_THEME_FALLBACK = "textual-dark"

# Настройки, после изменения которых mpv надо перезапустить.
RESTART_KEYS = {"mpv_path", "mpv_args", "ytdl"}

# Классическая панель: (id, действие, иконка в iconset, размер: s 6x3 / l 8x4 клетки, ключ подсказки)
CONTROLS = [
    ("ctl_shuffle", "shuffle", "shuffle", "s", "tip_shuffle"),
    ("ctl_prev", "prev", "prev", "s", "tip_prev"),
    ("ctl_back", "seek(-1)", "back", "s", "tip_back"),
    ("ctl_toggle", "toggle_pause", "play", "l", "tip_toggle"),
    ("ctl_fwd", "seek(1)", "fwd", "s", "tip_fwd"),
    ("ctl_next", "next", "next", "s", "tip_next"),
    ("ctl_repeat", "cycle_repeat", "repeat", "s", "tip_repeat"),
    ("ctl_voldn", "volume(-1)", "voldn", "s", "tip_voldn"),
    ("ctl_volup", "volume(1)", "volup", "s", "tip_volup"),
]
CONTROL_ACTIONS = {cid: action for cid, action, *_rest in CONTROLS}

# Мобильная раскладка. Иконки: (id, действие, имя в iconset, размер: s 6x3 / l 8x4 клетки)
MOBILE_ICONS = [
    ("m_shuffle", "shuffle", "shuffle", "s"),
    ("m_prev", "prev", "prev", "s"),
    ("m_toggle", "toggle_pause", "play", "l"),
    ("m_next", "next", "next", "s"),
    ("m_repeat", "cycle_repeat", "repeat", "s"),
]
# Нижний ряд мелких кнопок: (id, подпись, действие, ключ подсказки)
MOBILE_BUTTONS = [
    ("m_voldn", "−", "volume(-1)", "tip_voldn"),
    ("m_volup", "+", "volume(1)", "tip_volup"),
    ("m_queue", "≡", "toggle_queue", "tip_queue"),
    ("m_library", "♫", "open_library", "tip_library"),
    ("m_settings", "⚙", "settings", "tip_settings"),
]
MOBILE_BUTTON_ACTIONS = {bid: action for bid, _l, action, _t in MOBILE_BUTTONS}


# Действия, которые показываются в подвале: (действие, ключ перевода, как показывать клавишу)
FOOTER_ACTIONS = {
    "quit": ("footer_quit", "^Q"), "open_library": ("footer_library", "^O"),
    "toggle_pause": ("footer_pause", "Space"), "next": ("footer_next", None),
    "prev": ("footer_prev", None), "tracks": ("footer_tracks", None),
    "toggle_queue": ("footer_queue", None), "settings": ("footer_settings", "F2"),
}

# Остальные действия — без подписи, только горячая клавиша.
HIDDEN_ACTIONS = [
    "open_url", "seek(-1)", "seek(1)", "volume(1)", "volume(-1)", "mute", "cycle_repeat", "shuffle",
    "speed(-1)", "speed(1)", "speed(0)", "remove", "move(-1)", "move(1)", "clear_queue",
    "play_terminal", "cycle_layout",
]


MANAGED_ACTIONS = frozenset(FOOTER_ACTIONS) | frozenset(HIDDEN_ACTIONS)


def _priority(action: str, key: str) -> bool:
    """Выход по Ctrl+… работает даже в поле ввода; одиночная буква (q) там должна печататься."""
    return action == "quit" and key.startswith("ctrl+")


def make_bindings(lang: str, overrides: dict | None = None) -> list[Binding]:
    """Все клавиши приложения, по одному Binding на клавишу.

    overrides — «действие -> клавиша(и)» из настроек (несколько клавиш — через запятую).
    Один Binding на клавишу: так список можно пересобрать на лету и не зависеть от того,
    как Textual разбирает «a,b». Сначала идут действия подвала, затем их доп. клавиши и скрытые действия.
    """
    footer: list[Binding] = []
    extra: list[Binding] = []
    for action, (label_key, display) in FOOTER_ACTIONS.items():
        keys = effective_keys(action, overrides)
        footer.append(Binding(
            keys[0], action, tr(lang, label_key), show=True,
            key_display=display if keys[0] == KEYBINDING_DEFAULTS[action] else None,
            priority=_priority(action, keys[0])))
        extra += [Binding(key, action, "", show=False, priority=_priority(action, key)) for key in keys[1:]]
    for action in HIDDEN_ACTIONS:
        extra += [Binding(key, action, "", show=False) for key in effective_keys(action, overrides)]
    return footer + extra


_INITIAL_SETTINGS = load_settings()
_INITIAL_LANG = _INITIAL_SETTINGS.get("language", "ru")


class FiptApp(App):
    COMMANDS = {SettingsCommands}
    BINDINGS = make_bindings(_INITIAL_LANG, _INITIAL_SETTINGS.get("keybindings"))   # сохранённые клавиши — с первого запуска

    CSS = """
    #main_layout { layout: vertical; }
    #now_box { height: auto; padding: 0 1; border: round $primary; }
    #now_title { width: 1fr; height: 1; text-style: bold; }
    #now_sub { width: 1fr; height: 1; color: $text-muted; }
    #now_status { width: 1fr; height: 1; }
    #controls { height: auto; margin: 1 0; align: center middle; }
    #controls IconButton { width: 1fr; }
    #ctl_toggle { width: 2fr; }
    #queue { height: 1fr; min-height: 4; border: round $primary; }
    #queue.-b-heavy, #now_box.-b-heavy { border: heavy $primary; }
    #queue.-b-double, #now_box.-b-double { border: double $primary; }
    #queue.-b-solid, #now_box.-b-solid { border: solid $primary; }
    #queue.-b-dashed, #now_box.-b-dashed { border: dashed $primary; }
    #queue.-b-none, #now_box.-b-none { border: none; }
    #now_box.-b-round { border: round $primary; }
    #buttons_container { height: 3; }
    #buttons_container Button { margin-right: 1; }
    #log_box { height: 6; min-height: 3; }

    /* мобильная раскладка: .mobile / .classic показываются по классу -mobile на #main_layout */
    .mobile { display: none; }
    #main_layout.-mobile .mobile { display: block; }
    #main_layout.-mobile .classic { display: none; }
    #main_layout.-mobile #queue { display: none; }
    #main_layout.-mobile.-queue #queue { display: block; }
    #m_top { height: 1fr; align: center middle; padding: 0 2; }
    #main_layout.-queue #m_top { height: auto; }
    #m_title { width: 100%; height: auto; text-align: center; text-style: bold; }
    #m_artist { width: 100%; height: auto; text-align: center; color: $text-muted; }
    #m_bottom { height: auto; padding: 0 1; }
    #m_times { height: 1; }
    #m_pos { width: 1fr; }
    #m_rem { width: 1fr; text-align: right; }
    #m_controls { height: auto; margin: 1 0; align: center middle; }
    #m_toggle { width: 2fr; }
    #m_status { width: 100%; height: 1; text-align: center; color: $text-muted; }
    #m_buttons { height: 3; margin-top: 1; }
    #m_buttons FlatButton { min-width: 5; width: 1fr; margin-right: 1; }
    #m_log { width: 100%; height: 1; text-align: center; color: $text-muted; }
    """

    def __init__(self, targets: list[str] | None = None) -> None:
        super().__init__()
        self.settings = load_settings()
        self.lang: str = self.settings["language"]
        self._theme_defs: dict[str, dict] = themes.load_all()
        self._cli_targets = list(targets or [])
        self.player = MpvClient(
            on_property=self._on_property,
            on_event=self._on_event,
            on_exit=self._on_player_exit,
        )
        self._view_dirty = True
        self._queue_dirty = True
        self._queue_sig: list = []
        self._queue_cur = -1
        self._pending_highlight: int | None = None
        self._resume_index = 0
        self._quitting = False
        self._mpris = MprisService(self, on_error=self._on_mpris_error)
        self._shuffled_flag = False
        self._queue_names: list[str] = []
        self.tag_names: dict[str, str] = {}      # путь -> «Исполнитель — Название» из тегов ('' = тегов нет)
        self._tag_pending: set[str] = set()
        self._icon_sig: dict[str, tuple] = {}
        self._xf86_listener = XF86MediaKeys(self._on_xf86_action, self._on_xf86_error)

    # ───────────────────────── вспомогательное ─────────────────────────
    @property
    def _shuffled(self) -> bool:
        return self._shuffled_flag

    @_shuffled.setter
    def _shuffled(self, value: bool) -> None:
        if value != self._shuffled_flag:
            self._shuffled_flag = value
            self._mpris.schedule_flush()          # Shuffle виден снаружи через MPRIS

    @property
    def music_dir(self) -> str:
        return os.path.expanduser(self.settings["music_dir"])

    def _t(self, key: str, **kw) -> str:
        return tr(self.lang, key, **kw)

    def _log(self, key: str, **kw) -> None:
        text = self._t(key, **kw)
        self.log_box.write(text)
        # в мобильной раскладке журнала нет — последнее сообщение показываем одной строкой внизу
        short = " · ".join(ln.strip() for ln in text.strip().splitlines() if ln.strip())
        if short.startswith("[") and short.endswith("]"):
            short = short[1:-1]
        try:
            self.query_one("#m_log", Label).update(Text(short))
        except Exception:  # noqa: BLE001
            pass

    def _persist_settings(self) -> None:
        err = save_settings(self.settings)
        if err:
            self._log("log_settings_err", err=err)

    async def _cmd(self, *args, quiet: bool = False) -> bool:
        """Команда mpv с обработкой ошибок. quiet=True — не шуметь, если mpv простаивает."""
        try:
            await self.player.command(*args)
            return True
        except MpvError as e:
            reason = str(e)
            if reason in ("not_running", "closed"):
                self._log("log_player_down")
            elif not quiet:
                self._log("log_mpv_err", err=reason)
            return False

    async def _guard(self, coro) -> bool:
        try:
            await coro
            return True
        except MpvError as e:
            self._log("log_player_down" if str(e) in ("not_running", "closed") else "log_mpv_err", err=str(e))
            return False

    def _queue_entries(self) -> list[dict]:
        entries = self.player.props.get("playlist")
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def _is_idle(self) -> bool:
        return bool(self.player.props.get("idle-active", True)) or not self.player.running

    # ───────────────────────── интерфейс ─────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main_layout"):
            # ── классическая раскладка ──
            with Vertical(id="now_box", classes="classic"):
                yield Label("", id="now_title", markup=False)
                yield Label("", id="now_sub", markup=False)
                yield SeekBar(id="seek")
                yield Label("", id="now_status", markup=False)
            with Horizontal(id="controls", classes="classic"):
                for cid, action, _icon, _size, _tip in CONTROLS:
                    yield IconButton(action, id=cid)
            # ── мобильная: название и исполнитель по центру ──
            with Vertical(id="m_top", classes="mobile"):
                yield Label("", id="m_title", markup=False)
                yield Label("", id="m_artist", markup=False)
            yield OptionList(id="queue")          # общая очередь (в мобильной — по клавише L)
            # ── мобильная: полоса, время, кнопки ──
            with Vertical(id="m_bottom", classes="mobile"):
                yield SeekBar(id="m_seek")
                with Horizontal(id="m_times"):
                    yield Label("", id="m_pos", markup=False)
                    yield Label("", id="m_rem", markup=False)
                with Horizontal(id="m_controls"):
                    for cid, action, _icon, _size in MOBILE_ICONS:
                        yield IconButton(action, id=cid)
                yield Label("", id="m_status", markup=False)
                with Horizontal(id="m_buttons"):
                    for bid, label, _action, _tip in MOBILE_BUTTONS:
                        yield FlatButton(label, id=bid)
                yield Label("", id="m_log", markup=False)
            # ── классическая: нижние кнопки и журнал ──
            with Horizontal(id="buttons_container", classes="classic"):
                yield Button(self._t("btn_library"), variant="primary", id="open_library_btn")
                yield Button(self._t("btn_terminal"), variant="default", id="terminal_btn")
                yield Button(self._t("btn_settings"), variant="default", id="settings_btn")
            self.log_box = RichLog(wrap=True, id="log_box", classes="classic")
            yield self.log_box
        try:
            footer = Footer(show_command_palette=False)
        except TypeError:
            footer = Footer()
        yield footer

    async def on_mount(self) -> None:
        for data in self._theme_defs.values():
            self._register_theme_quiet(data)
        self.apply_theme(self.settings.get("theme", "pascal-dark"))
        self._apply_ui_settings()
        self._refresh_static_labels()
        self.query_one("#queue", OptionList).focus()
        self.set_interval(0.25, self._tick)

        if self.settings.get("mpris", True):
            self.run_worker(self._start_mpris(quiet=True), exclusive=False)
        if not await self._start_player():
            return
        if self._cli_targets:
            await self._load_cli_targets()
        elif self.settings.get("remember_queue", True):
            await self._restore_queue()
        if self.settings.get("xf86_media_keys", True):
            self._xf86_listener.start()

    async def on_unmount(self) -> None:
        self._xf86_listener.stop()
        await self._mpris.stop()
        await self.player.stop()

    def _apply_ui_settings(self) -> None:
        try:
            self.log_box.display = bool(self.settings.get("show_log", True))
        except Exception:  # noqa: BLE001
            pass
        self._apply_layout()
        self._apply_borders()
        for wid in ("#seek", "#m_seek"):                 # стиль полосы мог смениться
            try:
                self.query_one(wid).refresh()
            except Exception:  # noqa: BLE001
                pass

    def _apply_borders(self) -> None:
        """Стиль рамок очереди и блока «сейчас играет» — классом, чтобы цвет брался из темы."""
        style = self.settings.get("queue_border", "round")
        for wid in ("#queue", "#now_box"):
            try:
                widget = self.query_one(wid)
                for name in CHOICES["queue_border"]:
                    widget.set_class(name == style, f"-b-{name}")
            except Exception:  # noqa: BLE001
                pass

    def _apply_layout(self) -> None:
        """Включает/выключает мобильную раскладку и заставляет иконки перерисоваться."""
        mobile = self.settings.get("layout", "mobile") == "mobile"
        try:
            main = self.query_one("#main_layout")
            main.set_class(mobile, "-mobile")
            if not mobile:
                main.set_class(False, "-queue")
        except Exception:  # noqa: BLE001
            pass
        self._icon_sig.clear()
        self._view_dirty = True

    def _refresh_static_labels(self) -> None:
        self.title = self._t("app_title")
        self.query_one("#open_library_btn", Button).label = self._t("btn_library")
        self.query_one("#terminal_btn", Button).label = self._t("btn_terminal")
        self.query_one("#settings_btn", Button).label = self._t("btn_settings")
        for cid, _action, _icon, _size, tip in CONTROLS:
            try:
                self.query_one(f"#{cid}", IconButton).tooltip = self._t(tip)
            except Exception:  # noqa: BLE001
                pass
        for bid, _label, _action, tip in MOBILE_BUTTONS:
            try:
                self.query_one(f"#{bid}", FlatButton).tooltip = self._t(tip)
            except Exception:  # noqa: BLE001
                pass
        self._view_dirty = True

    # ───────────────────────── темы (как в PascalTUI) ─────────────────────────
    def _register_theme_quiet(self, data: dict) -> bool:
        theme = themes.to_textual_theme(data)
        if theme is None:
            return False
        try:
            self.register_theme(theme)
        except Exception:  # noqa: BLE001
            return False
        return True

    def theme_names(self) -> list[str]:
        try:
            return sorted(self.available_themes)
        except Exception:  # noqa: BLE001
            return sorted(self._theme_defs)

    def is_user_theme(self, name: str) -> bool:
        return bool(self._theme_defs.get(name, {}).get("_user", False))

    def theme_data(self, name: str) -> dict:
        if name in self._theme_defs:
            return {k: v for k, v in self._theme_defs[name].items() if not k.startswith("_")}
        data: dict = {"name": name, "dark": True}
        try:
            theme = self.available_themes[name]
            data["dark"] = bool(getattr(theme, "dark", True))
            for key in themes.COLOR_KEYS + themes.OPTIONAL_COLOR_KEYS:
                value = themes.normalize_hex(getattr(theme, key, None))
                if value:
                    data[key] = value
        except Exception:  # noqa: BLE001
            pass
        return data

    def register_user_theme(self, data: dict) -> bool:
        if not self._register_theme_quiet(data):
            return False
        entry = dict(data)
        entry["_user"] = True
        self._theme_defs[entry["name"]] = entry
        return True

    def remove_user_theme(self, name: str) -> None:
        themes.delete_user_theme(name)
        try:
            self.unregister_theme(name)
        except Exception:  # noqa: BLE001
            pass
        self._theme_defs = themes.load_all()
        for data in self._theme_defs.values():
            self._register_theme_quiet(data)
        if getattr(self, "theme", None) == name:
            self.apply_theme(DEFAULT_THEME_FALLBACK)
        self._log("log_theme_deleted", name=name)

    def apply_theme(self, name: str) -> None:
        if name in self.theme_names():
            try:
                self.theme = name
            except Exception:  # noqa: BLE001
                pass

    def preview_theme(self, name: str) -> None:
        self.apply_theme(name)

    # ───────────────────────── mpv: запуск и события ─────────────────────────
    async def _start_player(self) -> bool:
        err = await self.player.start(self.settings)
        self._queue_dirty = self._view_dirty = True
        if err is None:
            return True
        if err == "no_mpv":
            self._log("log_no_mpv", path=self.settings.get("mpv_path", "mpv"))
        elif err.startswith("exited:"):
            _, code, tail = (err.split(":", 2) + ["", ""])[:3]
            self._log("log_mpv_exited", code=code, info=tail)
        elif err == "no_socket":
            self._log("log_mpv_no_socket")
        else:
            self._log("log_io_error", err=err.removeprefix("os:"))
        return False

    def _on_property(self, name: str, data) -> None:
        self._mpris.on_property(name)
        if name in ("playlist", "playlist-pos"):
            self._queue_dirty = True
        elif name == "metadata":
            self._learn_current(data)
        self._view_dirty = True

    def _learn_current(self, metadata) -> None:
        """Теги играющего трека -> имя в очереди (работает и без mutagen, и для ссылок)."""
        path = self.player.props.get("path")
        name = tags.learn_from_metadata(metadata)
        if isinstance(path, str) and name and self.tag_names.get(path) != name:
            self.tag_names[path] = name
            self._queue_dirty = True

    def _on_event(self, event: dict) -> None:
        self._mpris.on_event(event)
        desc = describe_end_file(event)
        if desc:
            key, detail = desc
            self._log(key, detail=detail)

    def _on_player_exit(self) -> None:
        self._mpris.schedule_flush()
        if not self._quitting:
            self._log("log_mpv_died")
            self._view_dirty = self._queue_dirty = True

    async def _load_cli_targets(self) -> None:
        targets: list[str] = []
        for raw in self._cli_targets:
            path = os.path.expanduser(raw)
            if media.is_url(raw):
                targets.append(raw)
            elif os.path.isdir(path):
                targets += media.folder_media(path, recursive=True)
            elif os.path.exists(path):
                targets.append(os.path.abspath(path))
            else:
                self._log("log_bad_target", path=raw)
        if targets:
            self._shuffled = False
            await self._guard(self.player.replace_queue(targets, 0))

    async def _restore_queue(self) -> None:
        files, index = load_queue()
        if not files:
            return
        if await self._guard(self.player.enqueue(files)):
            self._resume_index = index
            self._log("log_queue_restored", n=len(files))

    # ───────────────────────── обновление экрана ─────────────────────────
    async def _tick(self) -> None:
        if self._queue_dirty:
            self._queue_dirty = False
            self._refresh_queue()
        if self._view_dirty:
            self._view_dirty = False
            self._refresh_view()

    def _set_icon(self, wid: str, name: str, size: str, state: str = "normal") -> None:
        """Ставит иконку на кнопку, только если она реально изменилась."""
        style = self.settings.get("icons", "text")
        sig = (name, size, state, style)
        if self._icon_sig.get(wid) != sig:
            self._icon_sig[wid] = sig
            self.query_one(wid).set_icon(get_icon(name, style, size), state)

    def _refresh_view(self) -> None:
        p = self.player.props
        running = self.player.running
        idle = self._is_idle()
        paused = bool(p.get("pause"))
        pos, dur = p.get("time-pos"), p.get("duration")
        mode = repeat_mode(p.get("loop-file"), p.get("loop-playlist"))
        playing_now = running and not idle
        artist = album = ""

        if not running:
            head = main = self._t("now_player_down")
            pos = dur = None
        elif idle:
            head = main = self._t("now_stopped" if self._queue_entries() else "now_idle")
            pos = dur = None
            self.sub_title = ""
        else:
            main, artist, album = media.split_now_playing(p.get("media-title") or "", p.get("metadata"))
            full = f"{artist} — {main}" if artist and main else main
            head = f"{'❚❚' if paused else '▶'} {full}"
            self.sub_title = full

        vol = p.get("volume")
        vol_txt = f"{int(round(vol))}%" if isinstance(vol, (int, float)) else "--"
        speed = p.get("speed")
        qpos = p.get("playlist-pos")
        n = len(self._queue_entries())
        index_txt = f"{qpos + 1}/{n}" if n and isinstance(qpos, int) and qpos >= 0 else ""

        # ── классическая раскладка ──
        self.query_one("#now_title", Label).update(Text(head))
        self.query_one("#now_sub", Label).update(Text(album))
        self.query_one("#seek", SeekBar).set_progress(pos, dur)
        if running:
            parts = [
                f"{media.format_time(p.get('time-pos'))} / {media.format_time(p.get('duration'))}",
                ("🔇 " if p.get("mute") else "🔊 ") + vol_txt,
                f"x{speed:g}" if isinstance(speed, (int, float)) else "x1",
                "↻ " + self._t(f"repeat_{mode}"),
            ]
            if self._shuffled:
                parts.append("⇄ " + self._t("shuffle_on"))
            if index_txt:
                parts.append(index_txt)
            classic_status = "  │  ".join(parts)
        else:
            classic_status = ""
        self.query_one("#now_status", Label).update(Text(classic_status))

        # ── мобильная раскладка ──
        self.query_one("#m_title", Label).update(Text(main))
        self.query_one("#m_artist", Label).update(Text(artist or album))
        self.query_one("#m_seek", SeekBar).set_progress(pos, dur)
        have_time = isinstance(pos, (int, float)) and isinstance(dur, (int, float))
        self.query_one("#m_pos", Label).update(Text(media.format_time(pos) if have_time else "0:00"))
        self.query_one("#m_rem", Label).update(
            Text("-" + media.format_time(max(dur - pos, 0)) if have_time else "--:--"))
        m_parts = [("🔇 " if p.get("mute") else "🔊 ") + vol_txt] if running else []
        if isinstance(speed, (int, float)) and speed != 1:
            m_parts.append(f"x{speed:g}")
        if mode != "off":
            m_parts.append("↻ " + self._t(f"repeat_{mode}"))
        if index_txt:
            m_parts.append(index_txt)
        self.query_one("#m_status", Label).update(Text("  ·  ".join(m_parts)))

        # ── иконки классической панели ──
        toggle_icon = "pause" if playing_now and not paused else "play"
        for cid, _action, icon, size, _tip in CONTROLS:
            state = "normal"
            if cid == "ctl_shuffle":
                state = "active" if self._shuffled else "dim"
            elif cid == "ctl_repeat":
                state = "active" if mode != "off" else "dim"
            elif cid == "ctl_toggle":
                icon = toggle_icon
            self._set_icon(f"#{cid}", icon, size, state)

        self._set_icon("#m_shuffle", "shuffle", "s", "active" if self._shuffled else "dim")
        self._set_icon("#m_prev", "prev", "s")
        self._set_icon("#m_toggle", "pause" if playing_now and not paused else "play", "l")
        self._set_icon("#m_next", "next", "s")
        self._set_icon("#m_repeat", "repeat", "s", "active" if mode != "off" else "dim")

    @staticmethod
    def _queue_prompt(index: int, name: str, playing: bool) -> Text:
        return Text(f"{'▶' if playing else ' '} {index + 1:>3}. {name}", no_wrap=True, overflow="ellipsis")

    def _entry_name(self, entry: dict) -> str:
        """Имя в очереди: из тегов (если включено и есть), иначе title от mpv / имя файла."""
        if self.settings.get("titles_from_tags", True):
            name = self.tag_names.get(str(entry.get("filename")))
            if name:
                return name
        return media.display_name(entry)

    def _refresh_queue(self) -> None:
        entries = self._queue_entries()
        files = [e.get("filename") for e in entries]
        names = [self._entry_name(e) for e in entries]
        cur = next((i for i, e in enumerate(entries) if e.get("current") or e.get("playing")), -1)
        ol = self.query_one("#queue", OptionList)
        focus: int | None = None

        if not files:
            self._shuffled = False
        if files != self._queue_sig:
            had_items = bool(self._queue_sig)
            prev = ol.highlighted
            self._queue_sig = files
            ol.clear_options()
            ol.add_options([Option(self._queue_prompt(i, n, i == cur)) for i, n in enumerate(names)])
            # после удаления/перемещения не прыгаем к текущему; при смене очереди — прыгаем
            keep = had_items and cur == self._queue_cur and prev is not None
            focus = min(prev, len(names) - 1) if keep else cur
        else:
            # очередь та же: обновляем только изменившиеся строки (пришли теги / сменился трек)
            changed = {i for i, (a, b) in enumerate(zip(names, self._queue_names)) if a != b}
            if cur != self._queue_cur:
                changed |= {self._queue_cur, cur}
                focus = cur
            for i in changed:
                if 0 <= i < len(names):
                    ol.replace_option_prompt_at_index(i, self._queue_prompt(i, names[i], i == cur))
        self._queue_cur = cur
        self._queue_names = names

        if self._pending_highlight is not None:
            focus = self._pending_highlight
            self._pending_highlight = None
        if focus is not None and 0 <= focus < len(names):
            ol.highlighted = focus
        self._kick_tag_loader(files)

    def _kick_tag_loader(self, files: list) -> None:
        """Запускает фоновое чтение тегов для файлов очереди, которых ещё нет в кэше."""
        if not self.settings.get("titles_from_tags", True) or not tags.available():
            return
        pending = [f for f in files if isinstance(f, str) and not media.is_url(f)
                   and f not in self.tag_names and f not in self._tag_pending]
        if pending:
            self._tag_pending.update(pending)
            self.run_worker(self._load_tags(pending), exclusive=False)

    async def _load_tags(self, paths: list[str]) -> None:
        for i in range(0, len(paths), 40):
            chunk = paths[i:i + 40]
            try:
                result = await asyncio.to_thread(tags.read_many, chunk)
            except Exception:  # noqa: BLE001
                result = {p: "" for p in chunk}
            self.tag_names.update(result)
            self._tag_pending.difference_update(chunk)
            self._queue_dirty = True          # строки обновятся порциями, без мигания всего списка

    # ───────────────────────── управление воспроизведением ─────────────────────────
    def _on_xf86_error(self, message: str) -> None:
        try:
            self.call_from_thread(self._log_external_message, message)
        except Exception:
            pass

    def _log_external_message(self, message: str) -> None:
        self.log_box.write(message)
        self.notify(message, title="XF86Audio", severity="warning", timeout=8)

    def _on_xf86_action(self, action: str) -> None:
        try:
            self.call_from_thread(self._dispatch_xf86_action, action)
        except Exception:
            pass

    def _dispatch_xf86_action(self, action: str) -> None:
        actions = {
            "toggle": self.action_toggle_pause, "stop": self.action_stop,
            "next": self.action_next, "prev": self.action_prev,
            "volume_up": lambda: self.action_volume(1),
            "volume_down": lambda: self.action_volume(-1), "mute": self.action_mute,
        }
        callback = actions.get(action)
        if callback:
            result = callback()
            if asyncio.iscoroutine(result):
                self.run_worker(result, group="xf86-media-keys", exclusive=False)

    async def action_stop(self) -> None:
        """Остановка воспроизведения. Очередь остаётся, Пауза/Space продолжит с этого же трека."""
        pos = self.player.props.get("playlist-pos")
        if isinstance(pos, int) and pos >= 0:
            self._resume_index = pos
        await self._cmd("stop", "keep-playlist", quiet=True)

    # ───────────────────────── MPRIS (управление снаружи) ─────────────────────────
    async def _start_mpris(self, quiet: bool) -> None:
        ok = await self._mpris.start()
        if not ok and not quiet:
            self.notify(self._t("log_mpris_failed"), title="MPRIS", severity="warning", timeout=8)

    def _on_mpris_error(self, message: str) -> None:
        try:
            self.log_box.write(message)
        except Exception:  # noqa: BLE001
            pass

    def mpris_view(self) -> PlayerView:
        return PlayerView.from_props(self.player.props, self._queue_entries(), self._shuffled, self.player.running)

    async def mpris_play(self) -> None:
        view = self.mpris_view()
        if view.idle:
            if view.queue_len:
                await self.action_toggle_pause()      # из простоя стартует с запомненного трека
        elif view.paused:
            await self._cmd("set_property", "pause", False, quiet=True)

    async def mpris_pause(self) -> None:
        view = self.mpris_view()
        if not view.idle and not view.paused:
            await self._cmd("set_property", "pause", True, quiet=True)

    async def mpris_play_pause(self) -> None:
        view = self.mpris_view()
        if view.running and (not view.idle or view.queue_len):
            await self.action_toggle_pause()

    async def mpris_stop(self) -> None:
        await self.action_stop()

    async def mpris_next(self) -> None:
        if self.mpris_view().can_next:
            await self.action_next()

    async def mpris_previous(self) -> None:
        if self.mpris_view().can_previous:
            await self.action_prev()

    async def mpris_seek(self, seconds: float, absolute: bool) -> None:
        await self._cmd("seek", seconds, "absolute" if absolute else "relative", quiet=True)

    async def mpris_set_volume(self, percent: float) -> None:
        lo, hi = INT_LIMITS["volume"]
        await self._cmd("set_property", "volume", max(lo, min(hi, percent)), quiet=True)

    async def mpris_set_rate(self, rate: float) -> None:
        await self._cmd("set_property", "speed", rate, quiet=True)

    async def mpris_set_repeat(self, mode: str) -> None:
        await self.action_set_repeat(mode)

    async def mpris_set_shuffle(self, on: bool) -> None:
        if on != self._shuffled:
            await self.action_shuffle()

    async def mpris_open(self, target: str) -> None:
        """OpenUri: добавляет в конец очереди и сразу играет (очередь пользователя не затирается)."""
        before = len(self._queue_entries())
        if not await self._guard(self.player.append([target])):
            return
        await self._cmd("set_property", "playlist-pos", before, quiet=True)
        await self._cmd("set_property", "pause", False, quiet=True)

    async def mpris_quit(self) -> None:
        await self.action_quit()

    async def action_toggle_pause(self) -> None:
        if not self.player.running:
            self._log("log_player_down")
            return
        if self._is_idle():
            entries = self._queue_entries()
            if entries:
                idx = min(self._resume_index, len(entries) - 1)
                await self._cmd("set_property", "pause", False)
                await self._cmd("set_property", "playlist-pos", idx)
            else:
                self.action_open_library()
            return
        await self._cmd("cycle", "pause", quiet=True)

    async def action_next(self) -> None:
        await self._cmd("playlist-next", "weak", quiet=True)

    async def action_prev(self) -> None:
        await self._cmd("playlist-prev", "weak", quiet=True)

    async def action_seek(self, direction: int) -> None:
        await self._cmd("seek", direction * int(self.settings.get("seek_step", 5)), "relative", quiet=True)

    async def action_volume(self, direction: int) -> None:
        await self._cmd("add", "volume", direction * int(self.settings.get("volume_step", 5)), quiet=True)

    async def action_mute(self) -> None:
        await self._cmd("cycle", "mute", quiet=True)

    async def action_speed(self, direction: int) -> None:
        if direction == 0:
            await self._cmd("set_property", "speed", 1.0, quiet=True)
        else:
            await self._cmd("add", "speed", 0.1 * direction, quiet=True)

    async def action_shuffle(self) -> None:
        """Случайный порядок вкл/выкл. Выкл возвращает прежний порядок (mpv >= 0.38)."""
        if not self._shuffled:
            if await self._cmd("playlist-shuffle", quiet=True):
                self._shuffled = True
                self._log("log_shuffled")
        else:
            self._shuffled = False
            try:
                await self.player.command("playlist-unshuffle")
                self._log("log_unshuffled")
            except MpvError:
                self._log("log_unshuffle_failed")
        self._view_dirty = True

    def action_toggle_queue(self) -> None:
        """Мобильная раскладка: показать/скрыть очередь. В классической очередь всегда на экране."""
        if len(self.screen_stack) > 1:
            return
        queue = self.query_one("#queue", OptionList)
        if self.settings.get("layout", "mobile") != "mobile":
            queue.focus()
            return
        main = self.query_one("#main_layout")
        show = not main.has_class("-queue")
        main.set_class(show, "-queue")
        if show:
            self._queue_dirty = True
            queue.focus()

    def action_cycle_layout(self) -> None:
        self.apply_changes({"layout": "classic" if self.settings.get("layout", "mobile") == "mobile" else "mobile"})

    async def on_icon_button_pressed(self, event: IconButton.Pressed) -> None:
        event.stop()
        await self.run_action(event.action)

    async def action_cycle_repeat(self) -> None:
        p = self.player.props
        mode = repeat_mode(p.get("loop-file"), p.get("loop-playlist"))
        await self.action_set_repeat(NEXT_REPEAT[mode])

    async def action_set_repeat(self, mode: str) -> None:
        if mode not in NEXT_REPEAT:
            return
        for name, value in repeat_properties(mode):
            if not await self._cmd("set_property", name, value, quiet=True):
                return

    async def on_seek_bar_seek(self, event: SeekBar.Seek) -> None:
        event.stop()
        await self._cmd("seek", event.fraction * 100, "absolute-percent", quiet=True)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "queue":
            return
        event.stop()
        await self._cmd("set_property", "pause", False, quiet=True)
        await self._cmd("set_property", "playlist-pos", event.option_index)

    # ───────────────────────── очередь ─────────────────────────
    def _highlighted_index(self) -> int | None:
        idx = self.query_one("#queue", OptionList).highlighted
        return idx if isinstance(idx, int) and 0 <= idx < len(self._queue_entries()) else None

    async def action_remove(self) -> None:
        idx = self._highlighted_index()
        if idx is not None:
            await self._cmd("playlist-remove", idx, quiet=True)

    async def action_move(self, delta: int) -> None:
        idx = self._highlighted_index()
        n = len(self._queue_entries())
        if idx is None:
            return
        if delta < 0 and idx > 0:
            # playlist-move A B: «A встаёт перед B»
            if await self._cmd("playlist-move", idx, idx - 1, quiet=True):
                self._pending_highlight = idx - 1
        elif delta > 0 and idx < n - 1:
            if await self._cmd("playlist-move", idx, idx + 2, quiet=True):
                self._pending_highlight = idx + 1

    def action_clear_queue(self) -> None:
        if len(self.screen_stack) > 1 or not self._queue_entries():
            return
        self.push_screen(
            ConfirmDialog(self._t("dlg_clear_title"), yes_label=self._t("dlg_yes"), no_label=self._t("dlg_no")),
            self._do_clear,
        )

    async def _do_clear(self, ok: bool | None) -> None:
        if ok:
            await self._cmd("stop", quiet=True)
            self._resume_index = 0
            self._shuffled = False

    async def action_tracks(self) -> None:
        if len(self.screen_stack) > 1:
            return
        if self._is_idle():
            self._log("log_nothing_playing")
            return
        self.push_screen(TracksScreen(self.lang, self.player.props.get("track-list")), self._apply_tracks)

    async def _apply_tracks(self, result: dict | None) -> None:
        if not result:
            return
        for prop in ("aid", "sid"):
            if prop in result:
                await self._cmd("set_property", prop, result[prop], quiet=True)

    # ───────────────────────── библиотека ─────────────────────────
    def action_open_library(self) -> None:
        if len(self.screen_stack) > 1:
            return
        start = os.path.expanduser(self.settings.get("last_dir") or self.settings["music_dir"])
        self.push_screen(BrowserScreen(start, self.lang), self._handle_library)

    def _targets_for(self, path: str, action: str) -> tuple[list[str], int]:
        """Что именно добавлять в очередь для выбранного пути. -> (список, с какого играть)."""
        if media.is_url(path):
            return [path], 0
        if os.path.isdir(path):
            return media.folder_media(path, recursive=True), 0
        if action == "play" and self.settings.get("play_folder", True) and not media.is_playlist(path):
            siblings = media.folder_media(os.path.dirname(path))
            if path in siblings:
                return siblings, siblings.index(path)
        return [path], 0

    async def _handle_library(self, result) -> None:
        if not result:
            return
        action, path, cur_dir = result
        self.settings["last_dir"] = cur_dir
        save_settings(self.settings)

        if action == "url":
            self._prompt("dlg_url_title", "https://", self._apply_url)
            return
        targets, start = self._targets_for(path, action)
        if not targets:
            self._log("log_no_media", path=path)
        elif action == "play":
            if await self._guard(self.player.replace_queue(targets, start)):
                self._shuffled = False
                self._log("log_playing", n=len(targets))
        else:
            if await self._guard(self.player.append(targets)):
                self._log("log_added", n=len(targets))
        if action != "play":
            self.action_open_library()  # как в PascalTUI: меню открывается снова

    async def _apply_url(self, value: str | None) -> None:
        if value and (media.is_url(value) or os.path.exists(os.path.expanduser(value))):
            target = value if media.is_url(value) else os.path.abspath(os.path.expanduser(value))
            if await self._guard(self.player.append([target])):
                self._log("log_added", n=1)
        elif value:
            self._log("log_bad_target", path=value)
        else:
            self.action_open_library()

    def action_open_url(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self._prompt("dlg_url_title", "https://", self._apply_url)

    # ───────────────────────── воспроизведение в терминале ─────────────────────────
    def action_play_terminal(self) -> None:
        if len(self.screen_stack) > 1:
            return
        entries = self._queue_entries()
        targets = [str(e["filename"]) for e in entries if e.get("filename")]
        if not targets:
            self._log("log_queue_empty")
            return
        pos = self.player.props.get("playlist-pos")
        self._terminal_session(targets, pos if isinstance(pos, int) and pos >= 0 else 0)

    @work(exclusive=True)
    async def _terminal_session(self, targets: list[str], start: int) -> None:
        await self._cmd("set_property", "pause", True, quiet=True)  # фоновый mpv замолкает
        with self.suspend():
            self._run_in_terminal(targets, start)
        self._view_dirty = True

    def _run_in_terminal(self, targets: list[str], start: int) -> None:
        os.system("clear")
        print(self._t("term_header", vo=self.settings.get("terminal_vo", "tct")))
        err = run_in_terminal(self.settings, targets, start)
        if err == "no_mpv":
            print(self._t("log_no_mpv", path=self.settings.get("mpv_path", "mpv")))
        elif err:
            print(self._t("term_exc", e=err))
        if err:
            try:
                input(self._t("term_enter"))
            except EOFError:
                pass

    # ───────────────────────── настройки ─────────────────────────
    def action_settings(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.action_command_palette()

    def _rebuild_bindings(self) -> None:
        """Применяет язык подписей и клавиши из настроек к уже запущенному приложению.

        Трогаем только действия fipt (MANAGED_ACTIONS): встроенные клавиши Textual
        (палитра команд Ctrl+P, Ctrl+C) остаются на месте.
        """
        try:
            table = self._bindings.key_to_bindings
            for key in list(table):
                kept = [b for b in table[key] if b.action not in MANAGED_ACTIONS]
                if kept:
                    table[key] = kept
                else:
                    del table[key]
            for binding in make_bindings(self.lang, self.settings.get("keybindings")):
                table.setdefault(binding.key, []).append(binding)
            self.refresh_bindings()
        except Exception as exc:  # noqa: BLE001 — не роняем приложение, но и не молчим
            self._warn_keys_failed(exc)

    def _warn_keys_failed(self, exc: Exception) -> None:
        text = f"{type(exc).__name__}: {exc}".replace("[", "(").replace("]", ")")     # скобки ломают разметку
        try:
            self.log_box.write("keybindings: " + text)
            self.notify(self._t("log_keys_failed", err=text), severity="error", timeout=8)
        except Exception:  # noqa: BLE001
            pass

    def action_open_keybindings(self) -> None:
        self.push_screen(KeybindingsScreen(self.lang, self.settings),
                         lambda result: self.apply_changes(result) if result else None)

    def apply_changes(self, changes: dict) -> None:
        old = dict(self.settings)
        self.settings = sanitize({**self.settings, **changes}, self.settings)
        self.lang = self.settings["language"]
        self._persist_settings()
        if "theme" in changes:
            self.apply_theme(self.settings["theme"])
        if self.lang != old["language"] or "keybindings" in changes:
            self._rebuild_bindings()
        self._apply_ui_settings()
        self._refresh_static_labels()
        self._queue_dirty = True
        changed = {k for k in changes if self.settings.get(k) != old.get(k)}
        if changed:
            self.run_worker(self._apply_runtime(changed), exclusive=False)
        self._log("log_settings_saved")

    async def _apply_runtime(self, changed: set[str]) -> None:
        """Изменения, которые надо донести до уже запущенного mpv."""
        if changed & RESTART_KEYS:
            self._log("log_restarting")
            await self._restart_player()
            return
        if "video" in changed:
            await self._cmd("set_property", "vid", "no" if self.settings["video"] == "off" else "auto", quiet=True)
        if "xf86_media_keys" in changed:
            if self.settings["xf86_media_keys"]:
                self._xf86_listener.start()
            else:
                self._xf86_listener.stop()
        if "mpris" in changed:
            if self.settings["mpris"]:
                await self._start_mpris(quiet=False)
            else:
                await self._mpris.stop()
        if "replaygain" in changed:
            await self._cmd("set_property", "replaygain", self.settings["replaygain"], quiet=True)

    async def _restart_player(self) -> None:
        """Перезапускает mpv, сохраняя очередь (позиция внутри трека теряется)."""
        p = self.player.props
        targets = [str(e["filename"]) for e in self._queue_entries() if e.get("filename")]
        pos = p.get("playlist-pos")
        idx = pos if isinstance(pos, int) and pos >= 0 else self._resume_index
        was_idle, was_paused = self._is_idle(), bool(p.get("pause"))
        await self.player.stop()
        self._shuffled = False
        self._queue_sig = []
        if not await self._start_player():
            return
        if targets:
            await self._guard(self.player.enqueue(targets))
            self._resume_index = min(idx, len(targets) - 1)
            if not was_idle:
                await self._cmd("set_property", "playlist-pos", self._resume_index, quiet=True)
                if was_paused:
                    await self._cmd("set_property", "pause", True, quiet=True)
        self._log("log_restarted")

    async def action_restart_player(self) -> None:
        await self._restart_player()

    def action_set_option(self, key: str, value) -> None:
        self.apply_changes({key: value})

    def action_open_themes(self) -> None:
        self.push_screen(
            ThemesScreen(self.lang, self.settings, getattr(self, "theme", DEFAULT_THEME_FALLBACK)),
            lambda r: self.apply_changes(r) if r else None,
        )

    def action_open_playback(self) -> None:
        self.push_screen(PlaybackScreen(self.lang, self.settings), lambda r: self.apply_changes(r) if r else None)

    def action_open_general(self) -> None:
        self.push_screen(GeneralScreen(self.lang, self.settings), lambda r: self.apply_changes(r) if r else None)

    def action_open_paths(self) -> None:
        self.push_screen(PathsScreen(self.lang, self.settings), self._apply_paths)

    def _apply_paths(self, result: dict | None) -> None:
        if not result:
            return
        changes = dict(result)
        music = os.path.expanduser(changes.get("music_dir", self.music_dir))
        if not os.path.isdir(music):
            self._log("log_music_not_found_path", path=music)
            changes.pop("music_dir", None)
        else:
            changes["music_dir"] = music
        self.apply_changes(changes)

    def _prompt(self, title_key: str, default: str, callback) -> None:
        self.push_screen(
            InputDialog(
                title=self._t(title_key),
                placeholder=default,
                default=default,
                ok_label=self._t("dlg_ok"),
                cancel_label=self._t("dlg_cancel"),
            ),
            callback,
        )

    def action_prompt_music_dir(self) -> None:
        self._prompt("dlg_music_title", self.music_dir, self._apply_music_dir)

    def _apply_music_dir(self, path: str | None) -> None:
        if not path:
            return
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            self._log("log_music_not_found_path", path=path)
            return
        self.apply_changes({"music_dir": path, "last_dir": path})

    def action_prompt_mpv_path(self) -> None:
        self._prompt("dlg_mpv_title", str(self.settings.get("mpv_path", "mpv")), self._apply_mpv_path)

    def _apply_mpv_path(self, path: str | None) -> None:
        if not path:
            return
        if resolve_binary({"mpv_path": path}) is None:
            self._log("log_no_mpv", path=path)
            return
        self.apply_changes({"mpv_path": path})

    def action_show_paths(self) -> None:
        self._log(
            "log_paths",
            mpv=resolve_binary(self.settings) or "—",
            music=self.music_dir,
            config=str(SETTINGS_PATH),
            sock=self.player.socket_path or "—",
        )

    # ───────────────────────── кнопки и выход ─────────────────────────
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid in MOBILE_BUTTON_ACTIONS:
            await self.run_action(MOBILE_BUTTON_ACTIONS[bid])
        elif bid == "open_library_btn":
            self.action_open_library()
        elif bid == "terminal_btn":
            self.action_play_terminal()
        elif bid == "settings_btn":
            self.action_settings()

    def _save_state(self) -> None:
        p = self.player.props
        if self.settings.get("remember_volume", True):
            vol = p.get("volume")
            if isinstance(vol, (int, float)):
                self.settings["volume"] = max(0, min(130, int(round(vol))))
        if self.settings.get("remember_queue", True) and self.player.running:
            files = [str(e["filename"]) for e in self._queue_entries() if e.get("filename")]
            pos = p.get("playlist-pos")
            err = save_queue(files, pos if isinstance(pos, int) and pos >= 0 else self._resume_index)
            if err:
                self._log("log_settings_err", err=err)
        save_settings(self.settings)

    async def action_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._save_state()
        await self.player.stop()
        self.exit()
