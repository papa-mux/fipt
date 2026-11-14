"""Optional global XF86Audio* hotkeys for X11/XWayland sessions.

This is deliberately separate from MPRIS: it listens to X11 key events and
forwards them to FIPT. It does not require root, but desktop environments may
already own some media-key grabs.
"""
from __future__ import annotations

import select
import threading
from typing import Callable


KEY_ACTIONS = {
    "XF86AudioPlay": "toggle",
    "XF86AudioPause": "toggle",
    "XF86AudioStop": "stop",
    "XF86AudioNext": "next",
    "XF86AudioPrev": "prev",
    "XF86AudioRaiseVolume": "volume_up",
    "XF86AudioLowerVolume": "volume_down",
    "XF86AudioMute": "mute",
}


class XF86MediaKeys:
    """Grab common XF86 media keys on the current X display."""

    def __init__(self, callback: Callable[[str], None], on_error: Callable[[str], None] | None = None):
        self.callback = callback
        self.on_error = on_error
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._display = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        if not __import__("os").environ.get("DISPLAY"):
            self._report("XF86Audio: переменные DISPLAY нет; X11-перехват недоступен.")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._listen, name="fipt-xf86-keys", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        display, self._display = self._display, None
        if display is not None:
            try:
                display.close()
            except Exception:
                pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _report(self, message: str) -> None:
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass

    def _listen(self) -> None:
        display = None
        try:
            from Xlib import X, XK, display as xdisplay, error

            display = xdisplay.Display()
            self._display = display
            root = display.screen().root
            keymap: dict[int, str] = {}
            failed: list[str] = []

            def capture_error(err, _request):
                if isinstance(err, error.BadAccess):
                    failed.append("busy")

            display.set_error_handler(capture_error)
            for keysym_name, action in KEY_ACTIONS.items():
                keysym = XK.string_to_keysym(keysym_name)
                keycode = display.keysym_to_keycode(keysym) if keysym else 0
                if not keycode:
                    continue
                failed.clear()
                root.grab_key(keycode, X.AnyModifier, True, X.GrabModeAsync, X.GrabModeAsync)
                display.sync()
                if failed:
                    self._report(f"Клавиша {keysym_name} уже перехвачена окружением рабочего стола.")
                    continue
                keymap[keycode] = action

            if not keymap:
                self._report("Не удалось зарегистрировать XF86Audio-клавиши. Проверьте X11 и конфликты хоткеев.")
                return

            while not self._stop.is_set():
                ready, _, _ = select.select([display.fileno()], [], [], 0.25)
                if not ready:
                    continue
                while display.pending_events():
                    event = display.next_event()
                    if event.type == X.KeyPress:
                        action = keymap.get(event.detail)
                        if action:
                            self.callback(action)
        except ImportError:
            self._report("Для XF86Audio в X11 установите необязательный пакет: pip install python-xlib")
        except Exception as exc:
            if not self._stop.is_set():
                self._report(f"Ошибка XF86Audio listener: {exc}")
        finally:
            if display is not None:
                try:
                    display.close()
                except Exception:
                    pass
            self._display = None
