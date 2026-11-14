"""MPRIS2: управление fipt снаружи (media-клавиши рабочего стола, playerctl, виджеты, KDE Connect, ...).

Сервис занимает на сессионной шине имя org.mpris.MediaPlayer2.fipt и по пути /org/mpris/MediaPlayer2
отдаёт интерфейсы org.mpris.MediaPlayer2 и org.mpris.MediaPlayer2.Player
(https://specifications.freedesktop.org/mpris-spec/latest/). Работает на собственном
минимальном клиенте D-Bus (dbus_min.py), поэтому доп. пакеты не нужны.

Состояние берётся из свойств mpv: приложение собирает PlayerView (чистая функция from_props,
тестируется без шины), сервис переводит его в свойства MPRIS и сам шлёт PropertiesChanged / Seeked.
Команды исполняет приложение (MprisHost) — те же действия, что и у клавиш в интерфейсе.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import quote, unquote, urlparse

from . import media
from .mpv import repeat_mode
from .dbus_min import ERR_INVALID_ARGS, DBusConnection, DBusError, Interface, Variant

OBJECT_PATH = "/org/mpris/MediaPlayer2"
BUS_NAME = "org.mpris.MediaPlayer2.fipt"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
TRACK_PREFIX = "/org/fipt/Track/"

URI_SCHEMES = ["file", "http", "https"]
MIME_TYPES = [
    "audio/mpeg", "audio/flac", "audio/x-flac", "audio/ogg", "audio/opus", "audio/x-opus+ogg",
    "audio/x-vorbis+ogg", "audio/aac", "audio/mp4", "audio/x-m4a", "audio/x-wav", "audio/wav",
    "audio/x-ms-wma", "audio/webm", "audio/x-matroska", "video/mp4", "video/x-matroska", "video/webm",
]

MIN_RATE, MAX_RATE = 0.25, 4.0
FLUSH_DELAY = 0.05          # объединяем пачку изменений mpv в один PropertiesChanged
VIEW_TTL = 0.02             # GetAll читает ~20 свойств подряд — считаем состояние один раз

# изменения этих свойств mpv могут поменять свойства MPRIS (time-pos сюда не входит: Position не рассылается)
WATCHED = frozenset({
    "pause", "duration", "volume", "media-title", "metadata", "playlist", "playlist-pos",
    "speed", "loop-file", "loop-playlist", "idle-active", "path", "seekable",
})

LOOP_TO_MODE = {"None": "off", "Track": "track", "Playlist": "playlist"}
MODE_TO_LOOP = {v: k for k, v in LOOP_TO_MODE.items()}


# ───────────────────────── состояние плеера (чистая логика) ─────────────────────────

@dataclass
class PlayerView:
    running: bool = False
    idle: bool = True
    paused: bool = False
    position: float | None = None      # секунды
    duration: float | None = None      # секунды
    volume: float = 0.0                # шкала mpv: 0..130
    speed: float = 1.0
    repeat: str = "off"                # off | track | playlist
    shuffle: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    path: str = ""
    queue_len: int = 0
    queue_pos: int = -1
    entry_id: int | None = None
    seekable: bool = True

    @classmethod
    def from_props(cls, props: dict, entries: list[dict], shuffled: bool, running: bool) -> "PlayerView":


        def num(value, default=None):
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        idle = bool(props.get("idle-active", True)) or not running
        pos = props.get("playlist-pos")
        pos = pos if isinstance(pos, int) and not isinstance(pos, bool) and 0 <= pos < len(entries) else -1
        if pos < 0:
            pos = next((i for i, e in enumerate(entries) if e.get("current") or e.get("playing")), -1)
        entry = entries[pos] if pos >= 0 else {}
        entry_id = entry.get("id") if isinstance(entry.get("id"), int) else (pos if pos >= 0 else None)
        title, artist, album = media.split_now_playing(props.get("media-title") or "", props.get("metadata"))
        path = props.get("path") if isinstance(props.get("path"), str) else str(entry.get("filename") or "")
        view = cls(
            running=running, idle=idle, paused=bool(props.get("pause")),
            position=num(props.get("time-pos")), duration=num(props.get("duration")),
            volume=num(props.get("volume"), 0.0), speed=num(props.get("speed"), 1.0) or 1.0,
            repeat=repeat_mode(props.get("loop-file"), props.get("loop-playlist")),
            shuffle=bool(shuffled), title=title, artist=artist, album=album, path=path,
            queue_len=len(entries), queue_pos=pos, entry_id=entry_id,
            seekable=props.get("seekable") is not False,
        )
        return view

    # ---- производные значения MPRIS ----
    @property
    def status(self) -> str:
        if self.idle:
            return "Stopped"
        return "Paused" if self.paused else "Playing"

    @property
    def track_id(self) -> str:
        if self.idle or self.entry_id is None:
            return NO_TRACK
        return f"{TRACK_PREFIX}{self.entry_id}"

    @property
    def position_us(self) -> int:
        if self.idle or self.position is None:
            return 0
        return max(0, int(self.position * 1_000_000))

    @property
    def length_us(self) -> int | None:
        if self.idle or self.duration is None or self.duration <= 0:
            return None
        return int(self.duration * 1_000_000)

    @property
    def can_next(self) -> bool:
        return self.running and self.queue_len > 0 and (self.repeat == "playlist" or self.queue_pos + 1 < self.queue_len) \
            and not self.idle

    @property
    def can_previous(self) -> bool:
        return self.running and not self.idle and (self.repeat == "playlist" or self.queue_pos > 0)

    @property
    def can_play(self) -> bool:
        return self.running and (not self.idle or self.queue_len > 0)

    @property
    def can_pause(self) -> bool:
        return self.running and not self.idle

    @property
    def can_seek(self) -> bool:
        return self.running and not self.idle and self.seekable and self.length_us is not None


def path_to_uri(path: str) -> str:
    """Файл -> file:// URI; ссылки остаются как есть."""
    if media.is_url(path):
        return path
    return "file://" + quote(os.path.abspath(os.path.expanduser(path)))


def uri_to_target(uri: str) -> str:
    """URI из OpenUri -> путь файла или ссылка для mpv. DBusError(InvalidArgs), если схема не поддержана."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise DBusError(ERR_INVALID_ARGS, f"remote file URI is not supported: {uri}")
        path = unquote(parsed.path)
        if not os.path.isfile(path):
            raise DBusError(ERR_INVALID_ARGS, f"no such file: {path}")
        return path
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return uri
    raise DBusError(ERR_INVALID_ARGS, f"unsupported URI: {uri}")


def build_metadata(view: PlayerView) -> dict[str, Variant]:
    meta: dict[str, Variant] = {"mpris:trackid": Variant("o", view.track_id)}
    if view.idle:
        return meta
    if view.length_us is not None:
        meta["mpris:length"] = Variant("x", view.length_us)
    if view.title:
        meta["xesam:title"] = Variant("s", view.title)
    if view.artist:
        meta["xesam:artist"] = Variant("as", [view.artist])
    if view.album:
        meta["xesam:album"] = Variant("s", view.album)
    if view.path:
        meta["xesam:url"] = Variant("s", path_to_uri(view.path))
    return meta


def player_properties(view: PlayerView) -> dict[str, Variant]:
    """Все свойства Player, кроме Position (она не рассылается — для неё есть Seeked)."""
    rate = min(max(view.speed, MIN_RATE), MAX_RATE)
    return {
        "PlaybackStatus": Variant("s", view.status),
        "LoopStatus": Variant("s", MODE_TO_LOOP.get(view.repeat, "None")),
        "Rate": Variant("d", rate),
        "Shuffle": Variant("b", view.shuffle),
        "Metadata": Variant("a{sv}", build_metadata(view)),
        "Volume": Variant("d", max(view.volume, 0.0) / 100.0),
        "MinimumRate": Variant("d", MIN_RATE),
        "MaximumRate": Variant("d", MAX_RATE),
        "CanGoNext": Variant("b", view.can_next),
        "CanGoPrevious": Variant("b", view.can_previous),
        "CanPlay": Variant("b", view.can_play),
        "CanPause": Variant("b", view.can_pause),
        "CanSeek": Variant("b", view.can_seek),
        "CanControl": Variant("b", True),
    }


# ───────────────────────── связь с приложением ─────────────────────────

class MprisHost(Protocol):
    """То, что сервис ждёт от приложения (его реализует FiptApp)."""

    def mpris_view(self) -> PlayerView: ...
    async def mpris_play(self) -> None: ...
    async def mpris_pause(self) -> None: ...
    async def mpris_play_pause(self) -> None: ...
    async def mpris_stop(self) -> None: ...
    async def mpris_next(self) -> None: ...
    async def mpris_previous(self) -> None: ...
    async def mpris_seek(self, seconds: float, absolute: bool) -> None: ...
    async def mpris_set_volume(self, percent: float) -> None: ...
    async def mpris_set_rate(self, rate: float) -> None: ...
    async def mpris_set_repeat(self, mode: str) -> None: ...
    async def mpris_set_shuffle(self, on: bool) -> None: ...
    async def mpris_open(self, target: str) -> None: ...
    async def mpris_quit(self) -> None: ...


class MprisService:
    def __init__(self, host: MprisHost, bus_name: str = BUS_NAME,
                 on_error: Callable[[str], None] | None = None,
                 paths: list[str] | None = None) -> None:
        self.host = host
        self.bus_name = bus_name
        self.on_error = on_error
        self._paths = paths
        self.bus: DBusConnection | None = None
        self.owned_name: str | None = None
        self.root = self._make_root()
        self.player = self._make_player()
        self._last: dict[str, Variant] = {}
        self._view: PlayerView | None = None
        self._view_at = -1.0
        self._flush_handle: asyncio.TimerHandle | None = None
        self._seeking = False
        self._tasks: set[asyncio.Task] = set()

    # ---- жизненный цикл ----
    @property
    def running(self) -> bool:
        return self.bus is not None and self.bus.connected and self.owned_name is not None

    async def start(self) -> bool:
        """True — сервис на шине. При недоступной шине возвращает False (и зовёт on_error)."""
        if self.running:
            return True
        bus = DBusConnection(on_close=self._on_bus_closed)
        try:
            await bus.connect(self._paths)
            name = self.bus_name
            if not await bus.request_name(name):
                name = f"{self.bus_name}.instance{os.getpid()}"     # второй fipt: так велит спецификация
                if not await bus.request_name(name):
                    raise ConnectionError(f"cannot own {name}")
        except Exception as exc:  # noqa: BLE001
            await bus.close()
            self._report(f"MPRIS: {exc}")
            return False
        bus.export(OBJECT_PATH, self.root, self.player)
        self.bus, self.owned_name = bus, name
        self._last = player_properties(self._get_view(fresh=True))
        return True

    async def stop(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        bus, self.bus = self.bus, None
        name, self.owned_name = self.owned_name, None
        for task in list(self._tasks):
            task.cancel()
        if bus is not None:
            bus.unexport(OBJECT_PATH)
            if name:
                await bus.release_name(name)
            await bus.close()

    def _on_bus_closed(self) -> None:
        self.bus, self.owned_name = None, None
        self._report("MPRIS: соединение с D-Bus потеряно")

    def _report(self, message: str) -> None:
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:  # noqa: BLE001
                pass

    # ---- уведомления от приложения ----
    def on_property(self, name: str) -> None:
        if name in WATCHED:
            self.schedule_flush()

    def on_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "seek":
            self._seeking = True
        elif kind == "playback-restart" and self._seeking:
            self._seeking = False
            self.emit_seeked()
        elif kind in ("start-file", "end-file", "idle"):
            self.schedule_flush()

    def schedule_flush(self) -> None:
        if not self.running or self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_handle = loop.call_later(FLUSH_DELAY, self.flush)

    def flush(self) -> None:
        """Отправляет PropertiesChanged только по реально изменившимся свойствам."""
        self._flush_handle = None
        if not self.running:
            return
        current = player_properties(self._get_view(fresh=True))
        changed = {k: v for k, v in current.items() if self._last.get(k) != v}
        self._last = current
        if changed and self.bus is not None:
            self.bus.properties_changed(OBJECT_PATH, IFACE_PLAYER, changed)

    def emit_seeked(self) -> None:
        if self.running and self.bus is not None:
            view = self._get_view(fresh=True)
            self.bus.emit_signal(OBJECT_PATH, IFACE_PLAYER, "Seeked", "x", (view.position_us,))

    # ---- состояние ----
    def _get_view(self, fresh: bool = False) -> PlayerView:
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            now = 0.0
        if fresh or self._view is None or now - self._view_at > VIEW_TTL:
            self._view, self._view_at = self.host.mpris_view(), now
        return self._view

    def _prop(self, name: str) -> Callable[[], Any]:
        return lambda: player_properties(self._get_view())[name].value

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self._report(f"MPRIS: {t.exception()}")
        task.add_done_callback(done)

    # ---- интерфейсы ----
    def _make_root(self) -> Interface:
        i = Interface(IFACE_ROOT)
        i.method("Raise", "", "", lambda: None)
        i.method("Quit", "", "", self._quit)
        i.prop("CanQuit", "b", lambda: True, emits="const")
        i.prop("CanRaise", "b", lambda: False, emits="const")
        i.prop("HasTrackList", "b", lambda: False, emits="const")
        i.prop("Identity", "s", lambda: "FIPT", emits="const")
        i.prop("SupportedUriSchemes", "as", lambda: list(URI_SCHEMES), emits="const")
        i.prop("SupportedMimeTypes", "as", lambda: list(MIME_TYPES), emits="const")
        return i

    def _quit(self) -> None:
        self._spawn(self.host.mpris_quit())          # ответ на вызов уйдёт раньше, чем закроется приложение

    def _make_player(self) -> Interface:
        i = Interface(IFACE_PLAYER)
        h = self.host
        i.method("Next", "", "", h.mpris_next)
        i.method("Previous", "", "", h.mpris_previous)
        i.method("Pause", "", "", h.mpris_pause)
        i.method("PlayPause", "", "", h.mpris_play_pause)
        i.method("Stop", "", "", h.mpris_stop)
        i.method("Play", "", "", h.mpris_play)
        i.method("Seek", "x", "", self._seek)
        i.method("SetPosition", "ox", "", self._set_position)
        i.method("OpenUri", "s", "", self._open_uri)
        i.signal("Seeked", "x")

        sigs = {k: v.signature for k, v in player_properties(PlayerView()).items()}
        setters = {
            "LoopStatus": self._set_loop, "Rate": self._set_rate,
            "Shuffle": lambda v: self._spawn(h.mpris_set_shuffle(bool(v))),
            "Volume": self._set_volume,
        }
        for name, sig in sigs.items():
            i.prop(name, sig, self._prop(name), setters.get(name),
                   emits="const" if name in ("MinimumRate", "MaximumRate", "CanControl") else "true")
        i.prop("Position", "x", lambda: self._get_view().position_us, emits="false")   # меняется только через Seeked
        return i

    async def _seek(self, offset_us: int) -> None:
        view = self._get_view(fresh=True)
        if view.can_seek:
            await self.host.mpris_seek(offset_us / 1_000_000, False)

    async def _set_position(self, track_id: str, position_us: int) -> None:
        view = self._get_view(fresh=True)
        if not view.can_seek or track_id != view.track_id:
            return                                    # спецификация: чужой трек — игнорируем
        length = view.length_us or 0
        if 0 <= position_us <= length:
            await self.host.mpris_seek(position_us / 1_000_000, True)

    async def _open_uri(self, uri: str) -> None:
        await self.host.mpris_open(uri_to_target(uri))

    def _set_loop(self, value: str) -> None:
        mode = LOOP_TO_MODE.get(value)
        if mode is None:
            raise DBusError(ERR_INVALID_ARGS, f"LoopStatus: {value!r}")
        self._spawn(self.host.mpris_set_repeat(mode))

    def _set_rate(self, value: float) -> None:
        if value <= 0:
            raise DBusError(ERR_INVALID_ARGS, "Rate must be positive")
        self._spawn(self.host.mpris_set_rate(min(max(value, MIN_RATE), MAX_RATE)))

    def _set_volume(self, value: float) -> None:
        if value != value:
            raise DBusError(ERR_INVALID_ARGS, "Volume is NaN")
        self._spawn(self.host.mpris_set_volume(max(value, 0.0) * 100.0))
