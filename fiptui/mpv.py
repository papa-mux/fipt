"""Управление mpv (без UI). Аналог runner.py из PascalTUI, но процесс живёт всё время работы.

mpv запускается как `mpv --idle=yes --input-ipc-server=<сокет>`, а мы говорим с ним по JSON IPC:
https://mpv.io/manual/master/#json-ipc  (unix-сокет: Linux, macOS, Termux; на Windows не работает).
Ошибки возвращаются кодами ("no_mpv", "os:<текст>", ...) — как в runner.py, чтобы UI мог перевести их.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from collections import deque
from typing import Any, Callable

COMMAND_TIMEOUT = 5.0
STARTUP_TIMEOUT = 6.0
_READ_LIMIT = 32 * 1024 * 1024  # свойство playlist с тысячами треков — одна очень длинная строка

# Свойства, за которыми следим: любое изменение приходит нам событием.
OBSERVED = (
    "pause", "time-pos", "duration", "volume", "mute", "media-title", "metadata",
    "playlist", "playlist-pos", "speed", "loop-file", "loop-playlist", "track-list",
    "idle-active", "path",
)


class MpvError(Exception):
    """Ошибка команды mpv (текст — код ошибки из IPC: 'property unavailable', ...)."""


# ───────────────────────── чистые функции (тестируются без mpv) ─────────────────────────

def resolve_binary(settings: dict) -> str | None:
    """Полный путь к mpv или None, если не найден."""
    name = os.path.expanduser(str(settings.get("mpv_path") or "mpv"))
    return shutil.which(name)


def split_extra_args(text: str) -> list[str]:
    try:
        return shlex.split(text or "")
    except ValueError:
        return []


def build_args(settings: dict, socket_path: str) -> list[str]:
    """Аргументы для фонового mpv (без самого бинарника)."""
    args = [
        "--idle=yes",
        "--no-terminal",
        "--force-window=no",
        "--audio-display=no",           # обложки не открывают окно
        f"--input-ipc-server={socket_path}",
        f"--volume={int(settings.get('volume', 80))}",
        f"--replaygain={settings.get('replaygain', 'no')}",
        "--ytdl=" + ("yes" if settings.get("ytdl", True) else "no"),
    ]
    if settings.get("video", "window") == "off":
        args.append("--no-video")
    args += split_extra_args(settings.get("mpv_args", ""))  # последними — пользователь важнее
    return args


def terminal_args(settings: dict, targets: list[str], start_index: int = 0) -> list[str]:
    """Аргументы для воспроизведения прямо в терминале (--vo=tct/kitty/sixel)."""
    vo = settings.get("terminal_vo", "tct")
    args = [
        f"--vo={vo}",
        f"--volume={int(settings.get('volume', 80))}",
        f"--replaygain={settings.get('replaygain', 'no')}",
        "--ytdl=" + ("yes" if settings.get("ytdl", True) else "no"),
    ]
    args += split_extra_args(settings.get("mpv_args", ""))
    if start_index > 0:
        args.append(f"--playlist-start={start_index}")
    return args + ["--"] + list(targets)


def run_in_terminal(settings: dict, targets: list[str], start_index: int = 0) -> str | None:
    """Блокирующий запуск mpv в текущем терминале. Вызывать внутри App.suspend().

    Возвращает код ошибки или None (как runner.run_program).
    """
    binary = resolve_binary(settings)
    if binary is None:
        return "no_mpv"
    try:
        subprocess.run([binary] + terminal_args(settings, targets, start_index))
    except OSError as e:
        return str(e)
    return None


def repeat_mode(loop_file, loop_playlist) -> str:
    """'off' | 'track' | 'playlist' по значениям свойств mpv (false/'no'/'inf'/число)."""
    def on(v) -> bool:
        return v not in (False, None, "no", 0, "0")
    if on(loop_file):
        return "track"
    if on(loop_playlist):
        return "playlist"
    return "off"


NEXT_REPEAT = {"off": "track", "track": "playlist", "playlist": "off"}


def repeat_properties(mode: str) -> list[tuple[str, str]]:
    """Какие свойства выставить, чтобы получить режим повтора."""
    return {
        "off": [("loop-file", "no"), ("loop-playlist", "no")],
        "track": [("loop-file", "inf"), ("loop-playlist", "no")],
        "playlist": [("loop-file", "no"), ("loop-playlist", "inf")],
    }[mode]


def tracks_of(track_list, kind: str) -> list[dict]:
    """Дорожки заданного типа ('audio' | 'sub' | 'video') из свойства track-list."""
    if not isinstance(track_list, list):
        return []
    return [t for t in track_list if isinstance(t, dict) and t.get("type") == kind]


def describe_track(track: dict) -> str:
    """'#2 · rus · Комментарии · ac3 · 6ch'. Без квадратных скобок: они ломают разметку Rich."""
    parts = [f"#{track.get('id', '?')}"]
    if track.get("lang"):
        parts.append(str(track["lang"]))
    if track.get("title"):
        parts.append(str(track["title"]))
    if track.get("codec"):
        parts.append(str(track["codec"]))
    channels = track.get("demux-channel-count")
    if isinstance(channels, int):
        parts.append(f"{channels}ch")
    return " · ".join(parts)


def describe_end_file(event: dict) -> tuple[str, str] | None:
    """Событие end-file -> (ключ локализации, подробности) либо None, если это не ошибка."""
    if event.get("event") != "end-file" or event.get("reason") != "error":
        return None
    detail = str(event.get("file_error") or "")
    if "unrecognized" in detail:
        return "log_err_format", detail
    if "loading failed" in detail or "open" in detail:
        return "log_err_open", detail
    return "log_err_play", detail


# ───────────────────────────────────── IPC-клиент ─────────────────────────────────────

class MpvClient:
    """Процесс mpv + JSON IPC. Работает в event loop приложения (Textual)."""

    def __init__(
        self,
        on_property: Callable[[str, Any], None] | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self.on_property = on_property
        self.on_event = on_event
        self.on_exit = on_exit
        self.props: dict[str, Any] = {}
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self.socket_path: str | None = None
        self._dir: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._err_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._closing = False
        self._pid: int | None = None
        atexit.register(self._atexit_kill)

    @property
    def running(self) -> bool:
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._proc is not None
            and self._proc.returncode is None
        )

    # ---- запуск / остановка ----
    async def start(self, settings: dict) -> str | None:
        """Запускает mpv. Возвращает код ошибки или None."""
        if self.running:
            return None
        binary = resolve_binary(settings)
        if binary is None:
            return "no_mpv"
        self._closing = False
        self.props.clear()
        self.stderr_tail.clear()
        self._dir = tempfile.mkdtemp(prefix="fiptui-")  # права 0700 — сокет чужим недоступен
        self.socket_path = os.path.join(self._dir, "mpv.sock")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                binary, *build_args(settings, self.socket_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self._cleanup_dir()
            return f"os:{e}"
        self._pid = self._proc.pid
        self._err_task = asyncio.ensure_future(self._drain_stderr(self._proc))

        err = await self._connect()
        if err:
            await self.stop()
            return err

        self._read_task = asyncio.ensure_future(self._read_loop())
        for i, name in enumerate(OBSERVED, start=1):
            self._issue_forget(["observe_property", i, name])
        return None

    async def _connect(self) -> str | None:
        assert self._proc is not None and self.socket_path is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_TIMEOUT
        while loop.time() < deadline:
            if self._proc.returncode is not None:
                tail = " | ".join(self.stderr_tail)
                return f"exited:{self._proc.returncode}:{tail}"
            if os.path.exists(self.socket_path):
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        self.socket_path, limit=_READ_LIMIT
                    )
                    return None
                except OSError:
                    pass
            await asyncio.sleep(0.05)
        return "no_socket"

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
        except Exception:  # noqa: BLE001
            return

    async def stop(self) -> None:
        """Корректно завершает mpv и освобождает ресурсы. Безопасно вызывать повторно."""
        self._closing = True
        if self.running:
            try:
                self._issue_forget(["quit"])  # ответа не будет — не ждём
                await self._writer.drain()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), 2.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), 2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
            except Exception:  # noqa: BLE001
                pass
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        for task in (self._read_task, self._err_task):
            if task is not None and not task.done():
                task.cancel()
        self._writer = self._reader = self._proc = None
        self._read_task = self._err_task = None
        self._pid = None
        self._fail_pending("closed")
        self._cleanup_dir()

    def _atexit_kill(self) -> None:
        """Страховка: если приложение упало, фоновый mpv (idle) не должен остаться жить."""
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGKILL)
            except OSError:
                pass
            self._pid = None
        self._cleanup_dir()

    def _cleanup_dir(self) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self.socket_path = None

    # ---- чтение ----
    async def _read_loop(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._fail_pending("closed")
            try:
                if self._writer is not None:
                    self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            if not self._closing and self.on_exit:
                self.on_exit()

    def _dispatch(self, msg: dict) -> None:
        if "event" not in msg and "request_id" in msg:
            fut = self._pending.pop(msg["request_id"], None)
            if fut is not None and not fut.done():
                if msg.get("error", "success") == "success":
                    fut.set_result(msg.get("data"))
                else:
                    fut.set_exception(MpvError(str(msg.get("error"))))
            return
        event = msg.get("event")
        if event == "property-change":
            name = msg.get("name")
            if not isinstance(name, str):
                return
            data = msg.get("data")  # поля нет, если свойство недоступно -> None
            self.props[name] = data
            if self.on_property:
                self.on_property(name, data)
        elif event and self.on_event:
            self.on_event(msg)

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(MpvError(reason))

    # ---- команды ----
    def _issue(self, args: list) -> asyncio.Future:
        """Синхронно кладёт команду в сокет (порядок вызовов = порядок исполнения)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        if not self.running or self._read_dead():
            fut.set_exception(MpvError("not_running"))   # mpv умер — не ждём таймаут
            return fut
        rid = self._next_id
        self._next_id += 1
        payload = json.dumps({"command": list(args), "request_id": rid}, ensure_ascii=False)
        try:
            self._writer.write(payload.encode("utf-8") + b"\n")
        except Exception as e:  # noqa: BLE001
            fut.set_exception(MpvError(f"write:{e}"))
            return fut
        self._pending[rid] = fut
        return fut

    def _read_dead(self) -> bool:
        """Читающая задача завершилась (mpv закрыл сокет), хотя запись ещё «открыта»."""
        return self._read_task is not None and self._read_task.done()

    def _issue_forget(self, args: list) -> None:
        """Команда без ожидания ответа (observe, quit). Ошибку молча забываем."""
        self._issue(args).add_done_callback(lambda f: f.cancelled() or f.exception())

    async def command(self, *args) -> Any:
        """Выполняет команду mpv. Бросает MpvError."""
        fut = self._issue(list(args))
        try:
            return await asyncio.wait_for(fut, COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            raise MpvError("timeout") from None

    async def batch(self, commands: list[list]) -> list[Any]:
        """Много команд подряд: все пишутся в сокет сразу, ответы ждём вместе.

        Возвращает список результатов; ошибка отдельной команды — объект MpvError в её позиции.
        """
        futs = [self._issue(c) for c in commands]
        if not futs:
            return []
        timeout = COMMAND_TIMEOUT + 0.02 * len(futs)
        try:
            return await asyncio.wait_for(asyncio.gather(*futs, return_exceptions=True), timeout)
        except asyncio.TimeoutError:
            raise MpvError("timeout") from None

    async def set_property(self, name: str, value: Any) -> None:
        await self.command("set_property", name, value)

    async def get_property(self, name: str) -> Any:
        return await self.command("get_property", name)

    # ---- удобные операции плеера ----
    async def replace_queue(self, targets: list[str], start_index: int = 0) -> None:
        """Заменяет очередь и начинает играть с start_index.

        mpv в режиме idle не стартует на `loadfile ... append`, поэтому порядок такой:
        stop (очищает очередь) -> append всех -> playlist-pos=start_index (запускает один раз).
        """
        if not targets:
            return
        cmds: list[list] = [["stop"]]
        cmds += [["loadfile", t, "append"] for t in targets]
        await self.batch(cmds)
        idx = max(0, min(start_index, len(targets) - 1))
        await self.set_property("playlist-pos", idx)
        await self.set_property("pause", False)

    async def enqueue(self, targets: list[str]) -> None:
        """Добавляет в очередь, НЕ начиная воспроизведение (для восстановления очереди)."""
        await self.batch([["loadfile", t, "append"] for t in targets])

    async def append(self, targets: list[str]) -> None:
        """Добавляет в конец очереди; если плеер простаивал — начинает играть."""
        if not targets:
            return
        idle = bool(self.props.get("idle-active", True))
        cmds: list[list] = [["loadfile", t, "append"] for t in targets]
        if idle:
            cmds[0] = ["loadfile", targets[0], "append-play"]
        await self.batch(cmds)
