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
)
from .settings import load_settings, sanitize, save_settings
from .widgets import FlatButton, SeekBar

DEFAULT_THEME_FALLBACK = "textual-dark"

# Настройки, после изменения которых mpv надо перезапустить.
RESTART_KEYS = {"mpv_path", "mpv_args", "ytdl"}

# (id кнопки, подпись, действие, ключ подсказки)
CONTROLS = [
    ("ctl_prev", "|◀", "prev", "tip_prev"),
    ("ctl_back", "«", "seek(-1)", "tip_back"),
    ("ctl_toggle", "▶❚❚", "toggle_pause", "tip_toggle"),
    ("ctl_fwd", "»", "seek(1)", "tip_fwd"),
    ("ctl_next", "▶|", "next", "tip_next"),
    ("ctl_voldn", "−", "volume(-1)", "tip_voldn"),
    ("ctl_volup", "+", "volume(1)", "tip_volup"),
    ("ctl_repeat", "↻", "cycle_repeat", "tip_repeat"),
    ("ctl_shuffle", "⇄", "shuffle", "tip_shuffle"),
]
CONTROL_ACTIONS = {cid: action for cid, _l, action, _t in CONTROLS}


def make_bindings(lang: str) -> list[Binding]:
    return [
        Binding("ctrl+q", "quit", tr(lang, "footer_quit"), key_display="^Q", priority=True),
        Binding("ctrl+o", "open_library", tr(lang, "footer_library"), key_display="^O"),
        Binding("space", "toggle_pause", tr(lang, "footer_pause"), key_display="Space"),
        Binding("n", "next", tr(lang, "footer_next")),
        Binding("p", "prev", tr(lang, "footer_prev")),
        Binding("t", "tracks", tr(lang, "footer_tracks")),
        Binding("f2", "settings", tr(lang, "footer_settings"), key_display="F2"),
        Binding("q", "quit", "", show=False),
        Binding("o", "open_library", "", show=False),
        Binding("s", "settings", "", show=False),
        Binding("u", "open_url", "", show=False),
        Binding("left", "seek(-1)", "", show=False),
        Binding("right", "seek(1)", "", show=False),
        Binding("plus,equals_sign", "volume(1)", "", show=False),
        Binding("minus", "volume(-1)", "", show=False),
        Binding("m", "mute", "", show=False),
        Binding("r", "cycle_repeat", "", show=False),
        Binding("x", "shuffle", "", show=False),
        Binding("left_square_bracket", "speed(-1)", "", show=False),
        Binding("right_square_bracket", "speed(1)", "", show=False),
        Binding("backslash", "speed(0)", "", show=False),
        Binding("delete", "remove", "", show=False),
        Binding("shift+up,K", "move(-1)", "", show=False),
        Binding("shift+down,J", "move(1)", "", show=False),
        Binding("c", "clear_queue", "", show=False),
        Binding("v", "play_terminal", "", show=False),
    ]


_INITIAL_LANG = load_settings().get("language", "ru")


class FiptApp(App):
    COMMANDS = {SettingsCommands}
    BINDINGS = make_bindings(_INITIAL_LANG)

    CSS = """
    #main_layout { layout: vertical; }
    #now_box { height: auto; padding: 0 1; }
    #now_title { width: 1fr; height: 1; text-style: bold; }
    #now_sub { width: 1fr; height: 1; color: $text-muted; }
    #now_status { width: 1fr; height: 1; }
    #controls { height: 3; margin: 1 0 0 0; }
    #controls FlatButton { min-width: 5; width: 1fr; margin-right: 1; }
    #queue { height: 1fr; min-height: 4; border: round $primary; }
    #buttons_container { height: 3; }
    #buttons_container Button { margin-right: 1; }
    #log_box { height: 6; min-height: 3; }
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
        self._shuffled = False
        self._queue_names: list[str] = []
        self.tag_names: dict[str, str] = {}      # путь -> «Исполнитель — Название» из тегов ('' = тегов нет)
        self._tag_pending: set[str] = set()

    # ───────────────────────── вспомогательное ─────────────────────────
    @property
    def music_dir(self) -> str:
        return os.path.expanduser(self.settings["music_dir"])

    def _t(self, key: str, **kw) -> str:
        return tr(self.lang, key, **kw)

    def _log(self, key: str, **kw) -> None:
        self.log_box.write(self._t(key, **kw))

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
            with Vertical(id="now_box"):
                yield Label("", id="now_title", markup=False)
                yield Label("", id="now_sub", markup=False)
                yield SeekBar(id="seek")
                yield Label("", id="now_status", markup=False)
            with Horizontal(id="controls"):
                for cid, label, _action, _tip in CONTROLS:
                    yield FlatButton(label, id=cid)
            yield OptionList(id="queue")
            with Horizontal(id="buttons_container"):
                yield Button(self._t("btn_library"), variant="primary", id="open_library_btn")
                yield Button(self._t("btn_terminal"), variant="default", id="terminal_btn")
                yield Button(self._t("btn_settings"), variant="default", id="settings_btn")
            self.log_box = RichLog(wrap=True, id="log_box")
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

        if not await self._start_player():
            return
        if self._cli_targets:
            await self._load_cli_targets()
        elif self.settings.get("remember_queue", True):
            await self._restore_queue()

    async def on_unmount(self) -> None:
        await self.player.stop()

    def _apply_ui_settings(self) -> None:
        try:
            self.log_box.display = bool(self.settings.get("show_log", True))
        except Exception:  # noqa: BLE001
            pass

    def _refresh_static_labels(self) -> None:
        self.title = self._t("app_title")
        self.query_one("#open_library_btn", Button).label = self._t("btn_library")
        self.query_one("#terminal_btn", Button).label = self._t("btn_terminal")
        self.query_one("#settings_btn", Button).label = self._t("btn_settings")
        for cid, _label, _action, tip in CONTROLS:
            try:
                self.query_one(f"#{cid}", FlatButton).tooltip = self._t(tip)
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
        desc = describe_end_file(event)
        if desc:
            key, detail = desc
            self._log(key, detail=detail)

    def _on_player_exit(self) -> None:
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

    def _refresh_view(self) -> None:
        p = self.player.props
        idle = self._is_idle()
        title_lbl = self.query_one("#now_title", Label)
        sub_lbl = self.query_one("#now_sub", Label)
        status = self.query_one("#now_status", Label)
        seek = self.query_one("#seek", SeekBar)

        if not self.player.running:
            title_lbl.update(Text(self._t("now_player_down")))
            sub_lbl.update(Text(""))
            seek.set_progress(None, None)
            status.update(Text(""))
            return
        if idle:
            title_lbl.update(Text(self._t("now_stopped" if self._queue_entries() else "now_idle")))
            sub_lbl.update(Text(""))
            seek.set_progress(None, None)
            self.sub_title = ""
        else:
            title, album = media.now_playing_text(p.get("media-title") or "", p.get("metadata"))
            icon = "❚❚" if p.get("pause") else "▶"
            title_lbl.update(Text(f"{icon} {title}"))
            sub_lbl.update(Text(album))
            seek.set_progress(p.get("time-pos"), p.get("duration"))
            self.sub_title = title

        vol = p.get("volume")
        vol_txt = f"{int(round(vol))}%" if isinstance(vol, (int, float)) else "--"
        speed = p.get("speed")
        mode = repeat_mode(p.get("loop-file"), p.get("loop-playlist"))
        parts = [
            f"{media.format_time(p.get('time-pos'))} / {media.format_time(p.get('duration'))}",
            ("🔇 " if p.get("mute") else "🔊 ") + vol_txt,
            f"x{speed:g}" if isinstance(speed, (int, float)) else "x1",
            "↻ " + self._t(f"repeat_{mode}"),
        ]
        if self._shuffled:
            parts.append("⇄ " + self._t("shuffle_on"))
        pos = p.get("playlist-pos")
        n = len(self._queue_entries())
        if n and isinstance(pos, int) and pos >= 0:
            parts.append(f"{pos + 1}/{n}")
        status.update(Text("  │  ".join(parts)))

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

    def _retranslate_bindings(self) -> None:
        try:
            for binding in make_bindings(self.lang):
                if binding.show:
                    self._bindings.key_to_bindings[binding.key] = [binding]
            self.refresh_bindings()
        except Exception:  # noqa: BLE001
            pass

    def apply_changes(self, changes: dict) -> None:
        old = dict(self.settings)
        self.settings = sanitize({**self.settings, **changes}, self.settings)
        self.lang = self.settings["language"]
        self._persist_settings()
        if "theme" in changes:
            self.apply_theme(self.settings["theme"])
        if self.lang != old["language"]:
            self._retranslate_bindings()
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
        if bid in CONTROL_ACTIONS:
            await self.run_action(CONTROL_ACTIONS[bid])
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
