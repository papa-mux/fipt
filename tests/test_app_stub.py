"""Тест логики app.py без настоящего Textual (заглушка) — но с реальным MpvClient и фейковым mpv.

Проверяется «клей»: очередь, диспетчеризация команд, перезапуск, сохранение состояния.
Отрисовка и клавиши Textual этим НЕ проверяются. Запуск: python3 tests/test_app_stub.py
"""
import asyncio
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp()
os.environ["HOME"] = HOME           # настройки и очередь теста не должны трогать реальный ~/.config


def _install_stub() -> None:
    class _Meta(type):
        def __getattr__(cls, attr):                      # Input.Changed, OptionList.OptionSelected, ...
            if attr.startswith("__"):
                raise AttributeError(attr)
            return type(attr, (Stub,), {})

    class Stub(metaclass=_Meta):
        def __init_subclass__(cls, **kw):
            super().__init_subclass__()

        def __init__(self, *a, **k):
            pass

        def __class_getitem__(cls, item):
            return cls

    def make_module(name):
        mod = types.ModuleType(name)
        mod.__path__ = []
        mod.__getattr__ = lambda attr: (_ for _ in ()).throw(AttributeError(attr)) if attr.startswith("__") \
            else type(attr, (Stub,), {})
        return mod

    for n in ("textual", "textual.app", "textual.binding", "textual.containers", "textual.events",
              "textual.screen", "textual.widgets", "textual.widgets.option_list", "textual.theme",
              "textual.command", "textual.message", "textual.widget"):
        sys.modules[n] = make_module(n)

    class Binding(Stub):
        def __init__(self, key, action, description="", show=True, key_display=None, **k):
            self.key, self.action, self.description, self.show = key, action, description, show
            self.key_display = key_display

    class Option(Stub):
        def __init__(self, prompt, id=None):
            self.prompt = prompt

    sys.modules["textual"].on = lambda *a, **k: (lambda f: f)
    sys.modules["textual"].work = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
    sys.modules["textual.binding"].Binding = Binding
    sys.modules["textual.widgets.option_list"].Option = Option
    if importlib.util.find_spec("rich") is None:
        class Text:
            def __init__(self, text="", **k):
                self.plain = text

            def append(self, s, style=None):
                self.plain += s
        rich, rich_text = make_module("rich"), types.ModuleType("rich.text")
        rich_text.Text = Text
        sys.modules["rich"], sys.modules["rich.text"] = rich, rich_text


_install_stub()
from fiptui import mpv as M  # noqa: E402
from fiptui import queue_store, tags  # noqa: E402
from fiptui.app import FiptApp, CONTROLS, CONTROL_ACTIONS, make_bindings  # noqa: E402


class FakeMutagen:
    """Подставной mutagen: теги по имени файла. t3 — без тегов, t4 — «битый» файл."""
    calls = 0

    @classmethod
    def File(cls, path, easy=False):
        cls.calls += 1
        name = os.path.basename(path)
        if name == "t4.mp3":
            raise RuntimeError("corrupt")
        if name == "t3.mp3":
            return types.SimpleNamespace(tags=None)
        if name == "t2.mp3":
            return types.SimpleNamespace(tags={"title": ["Only Title"]})
        return types.SimpleNamespace(tags={"artist": [f"Artist {name[1]}"], "title": [f"Song {name[1]}"]})


def plain(x):
    return getattr(x, "plain", x)


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.label = ""
        self.tooltip = ""

    def update(self, x):
        self.text = plain(x)


class FakeSeek:
    def __init__(self):
        self.progress = (None, None)

    def set_progress(self, pos, dur):
        self.progress = (pos, dur)


class FakeOptions:
    def __init__(self):
        self.options, self.highlighted, self.clears = [], None, 0

    def clear_options(self):
        self.options, self.highlighted = [], None
        self.clears += 1

    def add_options(self, opts):
        self.options += [plain(o.prompt) if hasattr(o, "prompt") else plain(o) for o in opts]

    def replace_option_prompt_at_index(self, i, prompt):
        self.options[i] = plain(prompt)

    def focus(self):
        pass


class FakeLog:
    def __init__(self):
        self.lines = []
        self.display = True

    def write(self, text):
        self.lines.append(text)

    def joined(self):
        return "".join(self.lines)


class AppUnderTest(FiptApp):
    """FiptApp без Textual: виджеты подменены, экраны записываются вместо показа."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.log_box = FakeLog()
        self.widgets = {"#now_title": FakeLabel(), "#now_sub": FakeLabel(), "#now_status": FakeLabel(),
                        "#seek": FakeSeek(), "#queue": FakeOptions()}
        self.pushed, self.exited, self.workers = [], False, []
        self.screen_stack = [object()]
        self.title = self.sub_title = ""

    def query_one(self, selector, cls=None):
        return self.widgets.setdefault(selector, FakeLabel())

    def push_screen(self, screen, callback=None):
        self.pushed.append((screen, callback))

    def run_worker(self, coro, exclusive=False):
        task = asyncio.ensure_future(coro)
        self.workers.append(task)
        return task

    def exit(self):
        self.exited = True

    def apply_theme(self, name):
        pass

    async def run_action(self, action):
        m = re.fullmatch(r"(\w+)(?:\((-?\d+)\))?", action)
        fn = getattr(self, "action_" + m.group(1))
        res = fn(int(m.group(2))) if m.group(2) is not None else fn()
        if asyncio.iscoroutine(res):
            await res

    async def settle(self, cond=lambda: True, timeout=3.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            await self._tick()
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.binary = os.path.join(self.tmp, "mpv")
        with open(self.binary, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'fake_mpv.py')} \"$@\"\n")
        os.chmod(self.binary, os.stat(self.binary).st_mode | stat.S_IEXEC)
        self.music = os.path.join(self.tmp, "music")
        os.makedirs(self.music)
        self.files = [os.path.join(self.music, f"t{i}.mp3") for i in range(5)]
        for f in self.files:
            open(f, "w").close()
        for leftover in (queue_store.paths.QUEUE_PATH, queue_store.paths.SETTINGS_PATH):
            if os.path.exists(leftover):          # каждый тест стартует с чистыми настройками
                os.remove(leftover)
        self.app = AppUnderTest()
        self.app.settings["mpv_path"] = self.binary
        self.app.settings["music_dir"] = self.music
        self.q = self.app.widgets["#queue"]
        self.assertTrue(await self.app._start_player())

    async def asyncTearDown(self):
        await self.app.player.stop()

    def log(self):
        return self.app.log_box.joined()

    async def play_from(self, i):
        await self.app._handle_library(("play", self.files[i], self.music))
        self.assertTrue(await self.app.settle(lambda: self.app.player.props.get("playlist-pos") == i
                                              and len(self.q.options) == 5))

    # ---- старт ----
    async def test_idle_view(self):
        await self.app.settle()
        self.assertIn("O", self.app.widgets["#now_title"].text)       # подсказка «нажмите O»
        self.assertEqual(self.q.options, [])

    async def test_no_mpv_is_reported(self):
        app = AppUnderTest()
        app.settings["mpv_path"] = "no-such-mpv-xyz"
        self.assertFalse(await app._start_player())
        self.assertIn("no-such-mpv-xyz", app.log_box.joined())
        await app.settle()
        self.assertIn("mpv", app.widgets["#now_title"].text)           # «mpv не запущен»
        await app._cmd("stop")                                         # команда без плеера не роняет приложение
        self.assertIn("mpv", app.log_box.joined())

    # ---- библиотека ----
    async def test_play_file_queues_folder(self):
        await self.play_from(2)
        self.assertEqual([e["filename"] for e in self.app.player.props["playlist"]], self.files)
        self.assertTrue(self.q.options[2].startswith("▶"))
        self.assertFalse(self.q.options[1].startswith("▶"))
        self.assertEqual(self.q.highlighted, 2)                        # подсветка встала на играющий
        self.assertTrue(self.app.widgets["#now_title"].text.startswith("▶"))
        self.assertIn("3/5", self.app.widgets["#now_status"].text)
        self.assertEqual(self.app.settings["last_dir"], self.music)

    async def test_play_folder_off_plays_single(self):
        self.app.settings["play_folder"] = False
        await self.app._handle_library(("play", self.files[3], self.music))
        self.assertTrue(await self.app.settle(lambda: len(self.app.player.props.get("playlist", [])) == 1))

    async def test_add_appends_and_reopens_library(self):
        await self.play_from(0)
        before = len(self.app.pushed)
        extra = os.path.join(self.music, "sub")
        os.makedirs(extra)
        for n in ("b.mp3", "a.mp3"):
            open(os.path.join(extra, n), "w").close()
        await self.app._handle_library(("add", extra, self.music))
        self.assertTrue(await self.app.settle(lambda: len(self.q.options) == 7))
        self.assertTrue(self.q.options[5].endswith("a.mp3"))          # папка добавлена по порядку
        self.assertEqual(self.app.player.props["playlist-pos"], 0)     # текущий трек не перебит
        self.assertEqual(len(self.app.pushed), before + 1)             # библиотека открыта снова

    async def test_empty_folder_reports(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        await self.app._handle_library(("add", empty, self.tmp))
        self.assertIn(empty, self.log())

    async def test_url_prompt_and_apply(self):
        await self.app._handle_library(("url", "", self.music))
        self.assertTrue(self.app.pushed)                               # диалог ввода показан
        await self.app._apply_url("https://example.com/a.mp3")
        self.assertTrue(await self.app.settle(lambda: len(self.q.options) == 1))
        self.assertTrue(self.q.options[0].endswith("a.mp3"))
        await self.app._apply_url("definitely not a path")
        self.assertIn("definitely not a path", self.log())

    # ---- названия из тегов ----
    def use_mutagen(self):
        FakeMutagen.calls = 0
        sys.modules["mutagen"] = FakeMutagen
        self.addCleanup(sys.modules.pop, "mutagen", None)

    async def test_names_from_tags_update_in_place(self):
        self.use_mutagen()
        await self.play_from(0)
        self.assertTrue(await self.app.settle(lambda: self.q.options[1].endswith("Artist 1 — Song 1")))
        self.assertTrue(self.q.options[2].endswith("Only Title"))        # только название — без исполнителя
        self.assertTrue(self.q.options[3].endswith("t3.mp3"))            # тегов нет — имя файла
        self.assertTrue(self.q.options[4].endswith("t4.mp3"))            # битый файл не ломает очередь
        self.assertTrue(self.q.options[0].startswith("▶"))               # маркер играющего сохранился
        self.assertEqual(self.q.clears, 1)                               # обновление построчное, без пересборки
        self.assertEqual(FakeMutagen.calls, 5)
        await self.app.settle(lambda: False, timeout=0.2)
        self.assertEqual(FakeMutagen.calls, 5)                           # повторно не читаем

    async def test_tags_disabled_setting(self):
        self.use_mutagen()
        self.app.settings["titles_from_tags"] = False
        await self.play_from(0)
        await self.app.settle(lambda: False, timeout=0.3)
        self.assertTrue(self.q.options[1].endswith("t1.mp3"))
        self.assertEqual(FakeMutagen.calls, 0)
        self.app.apply_changes({"titles_from_tags": True})               # включили — имена подтянулись
        self.assertTrue(await self.app.settle(lambda: self.q.options[1].endswith("Artist 1 — Song 1")))

    async def test_without_mutagen_filenames_then_learn_from_playing(self):
        self.assertFalse(tags.available())
        await self.play_from(1)
        self.assertEqual(self.app.workers, [])                           # читать нечем — фонового чтения нет
        self.assertTrue(self.q.options[3].endswith("t3.mp3"))
        self.app._on_property("metadata", {"ARTIST": "Z", "title": "Learned"})   # mpv отдал теги играющего
        self.assertTrue(await self.app.settle(lambda: self.q.options[1].endswith("Z — Learned")))
        self.assertTrue(self.q.options[0].endswith("t0.mp3"))
        self.app._on_property("metadata", None)                          # теги пропали — имя не затираем
        await self.app.settle(lambda: False, timeout=0.1)
        self.assertTrue(self.q.options[1].endswith("Z — Learned"))

    async def test_mojibake_title_repaired_everywhere(self):
        await self.play_from(1)
        bad = "Чужой — Концерт".encode("utf-8").decode("latin-1")       # как на скриншоте пользователя
        self.app.player.props["media-title"] = bad
        self.app.player.props["metadata"] = {"title": bad, "album": bad}
        self.app._on_property("metadata", self.app.player.props["metadata"])
        self.assertTrue(await self.app.settle(lambda: self.q.options[1].endswith("Чужой — Концерт")))
        self.assertIn("Чужой — Концерт", self.app.widgets["#now_title"].text)
        self.assertEqual(self.app.widgets["#now_sub"].text, "Чужой — Концерт")
        self.assertIn("Чужой — Концерт", self.app.sub_title)
        self.assertNotIn("Ð", self.app.widgets["#now_title"].text + self.q.options[1])

    async def test_learn_url_title(self):
        await self.app._apply_url("https://example.com/watch?v=1")
        self.assertTrue(await self.app.settle(lambda: self.app.player.props.get("playlist-pos") == 0))
        self.app._on_property("metadata", {"title": "Video Title"})
        self.assertTrue(await self.app.settle(lambda: self.q.options[0].endswith("Video Title")))

    # ---- перемешивание ----
    async def test_shuffle_toggle_restores_order(self):
        await self.play_from(0)
        names = lambda: [e["filename"] for e in self.app.player.props["playlist"]]
        await self.app.action_shuffle()
        self.assertTrue(await self.app.settle(lambda: names() == self.files[::-1]))
        self.assertTrue(self.app._shuffled)
        self.assertTrue(await self.app.settle(lambda: "⇄" in self.app.widgets["#now_status"].text))
        self.assertIn(self.app._t("log_shuffled").strip(), self.log())
        await self.app.action_shuffle()                                  # второй раз — вернуть порядок
        self.assertTrue(await self.app.settle(lambda: names() == self.files))
        self.assertFalse(self.app._shuffled)
        self.assertTrue(await self.app.settle(lambda: "⇄" not in self.app.widgets["#now_status"].text))
        self.assertIn(self.app._t("log_unshuffled").strip(), self.log())

    async def test_unshuffle_failure_is_reported(self):
        await self.play_from(0)
        self.app._shuffled = True                                        # как будто перемешали давно / mpv не умеет
        await self.app.action_shuffle()
        self.assertFalse(self.app._shuffled)
        self.assertIn(self.app._t("log_unshuffle_failed").strip(), self.log())

    async def test_new_queue_resets_shuffle_flag(self):
        await self.play_from(0)
        await self.app.action_shuffle()
        self.assertTrue(self.app._shuffled)
        await self.app._handle_library(("play", self.files[1], self.music))
        self.assertFalse(self.app._shuffled)

    # ---- управление ----
    async def test_next_prev_stop_at_edges_quietly(self):
        await self.play_from(0)
        await self.app.action_prev()                                   # на первом треке — «weak», без ошибки
        await self.app.action_next()
        self.assertTrue(await self.app.settle(lambda: self.app.player.props["playlist-pos"] == 1))
        self.assertNotIn("mpv error", self.log())

    async def test_seek_volume_speed_mute_pause(self):
        await self.play_from(0)
        p = self.app.player.props
        await self.app.run_action("seek(-1)")
        self.assertEqual(await self.app.player.get_property("last-seek"), [-5, "relative"])
        await self.app.on_seek_bar_seek(types.SimpleNamespace(fraction=0.25, stop=lambda: None))
        self.assertEqual(await self.app.player.get_property("last-seek"), [25.0, "absolute-percent"])
        await self.app.run_action("volume(1)")
        self.assertTrue(await self.app.settle(lambda: p["volume"] == 85))
        await self.app.run_action("speed(1)")
        self.assertTrue(await self.app.settle(lambda: p["speed"] == 1.1))
        await self.app.run_action("speed(0)")
        self.assertTrue(await self.app.settle(lambda: p["speed"] == 1.0))
        await self.app.action_mute()
        self.assertTrue(await self.app.settle(lambda: p["mute"] is True))
        self.assertTrue(await self.app.settle(lambda: "🔇" in self.app.widgets["#now_status"].text))
        await self.app.action_toggle_pause()
        self.assertTrue(await self.app.settle(lambda: p["pause"] is True))
        self.assertTrue(await self.app.settle(lambda: self.app.widgets["#now_title"].text.startswith("❚❚")))

    async def test_repeat_cycle_and_status(self):
        await self.play_from(0)
        p = self.app.player.props
        seen = []
        for _ in range(3):
            await self.app.action_cycle_repeat()
            await self.app.settle(lambda: False, timeout=0.15)
            from fiptui.mpv import repeat_mode
            seen.append(repeat_mode(p.get("loop-file"), p.get("loop-playlist")))
        self.assertEqual(seen, ["track", "playlist", "off"])

    async def test_selecting_option_plays_it(self):
        await self.play_from(0)
        ev = types.SimpleNamespace(option_list=types.SimpleNamespace(id="queue"), option_index=3, stop=lambda: None)
        await self.app.on_option_list_option_selected(ev)
        self.assertTrue(await self.app.settle(lambda: self.app.player.props["playlist-pos"] == 3))
        other = types.SimpleNamespace(option_list=types.SimpleNamespace(id="x"), option_index=1, stop=lambda: None)
        await self.app.on_option_list_option_selected(other)           # чужой список игнорируется
        await self.app.settle(lambda: False, timeout=0.15)
        self.assertEqual(self.app.player.props["playlist-pos"], 3)

    async def test_space_on_idle_queue_starts_from_resume_index(self):
        await self.app.player.enqueue(self.files)
        self.app._resume_index = 2
        self.assertTrue(await self.app.settle(lambda: len(self.q.options) == 5))
        self.assertIn("Space", make_bindings("en")[2].key_display)
        await self.app.action_toggle_pause()
        self.assertTrue(await self.app.settle(lambda: self.app.player.props["playlist-pos"] == 2))

    # ---- очередь ----
    async def test_remove_move_shuffle(self):
        await self.play_from(0)
        names = lambda: [e["filename"] for e in self.app.player.props["playlist"]]
        self.q.highlighted = 3
        await self.app.action_remove()
        self.assertTrue(await self.app.settle(lambda: len(self.q.options) == 4))
        self.assertNotIn(self.files[3], names())

        self.q.highlighted = 1
        await self.app.action_move(1)                                  # вниз
        self.assertTrue(await self.app.settle(lambda: names()[2] == self.files[1]))
        self.assertTrue(await self.app.settle(lambda: self.q.highlighted == 2))   # подсветка поехала за треком
        await self.app.action_move(-1)                                 # вверх
        self.assertTrue(await self.app.settle(lambda: names()[1] == self.files[1]))
        self.q.highlighted = 0
        await self.app.action_move(-1)                                 # за границей — ничего
        self.assertEqual(names()[0], self.files[0])

        await self.app.action_shuffle()
        self.assertTrue(await self.app.settle(lambda: names()[0] == self.files[4]))
        self.assertIn(self.app._t("log_shuffled").strip(), self.log())

    async def test_remove_keeps_highlight_position(self):
        await self.play_from(0)
        self.q.highlighted = 3
        await self.app.action_remove()
        self.assertTrue(await self.app.settle(lambda: len(self.q.options) == 4))
        self.assertEqual(self.q.highlighted, 3)                        # не прыгаем к текущему треку

    async def test_clear(self):
        await self.play_from(0)
        await self.app._do_clear(False)
        self.assertEqual(len(self.app.player.props["playlist"]), 5)
        await self.app._do_clear(True)
        self.assertTrue(await self.app.settle(lambda: self.q.options == []))
        self.assertTrue(await self.app.settle(lambda: "O" in self.app.widgets["#now_title"].text))

    async def test_tracks_applied(self):
        await self.play_from(0)
        await self.app._apply_tracks({"aid": 2, "sid": "no"})
        self.assertEqual(await self.app.player.get_property("aid"), 2)
        self.assertEqual(await self.app.player.get_property("sid"), "no")
        await self.app._apply_tracks(None)

    # ---- настройки, перезапуск, выход ----
    async def test_restart_keeps_queue_and_position(self):
        await self.play_from(3)
        old_sock = self.app.player.socket_path
        self.app.apply_changes({"mpv_args": "--some-flag"})
        self.assertTrue(self.app.workers)
        await asyncio.wait_for(self.app.workers[-1], 10)
        self.assertNotEqual(self.app.player.socket_path, old_sock)
        self.assertTrue(await self.app.settle(lambda: self.app.player.props.get("playlist-pos") == 3
                                              and len(self.q.options) == 5))
        self.assertEqual([e["filename"] for e in self.app.player.props["playlist"]], self.files)
        self.assertIn(self.app._t("log_restarting").strip(), self.log())
        self.assertIn(self.app._t("log_restarted").strip(), self.log())
        self.assertEqual(self.app.settings["mpv_args"], "--some-flag")

    async def test_non_restart_change_does_not_restart(self):
        await self.play_from(0)
        sock = self.app.player.socket_path
        self.app.apply_changes({"replaygain": "track", "seek_step": 10})
        await asyncio.wait_for(self.app.workers[-1], 5)
        self.assertEqual(self.app.player.socket_path, sock)
        self.assertEqual(await self.app.player.get_property("replaygain"), "track")
        await self.app.run_action("seek(1)")
        self.assertEqual(await self.app.player.get_property("last-seek"), [10, "relative"])

    async def test_unchanged_setting_schedules_nothing(self):
        n = len(self.app.workers)
        self.app.apply_changes({"mpv_args": self.app.settings["mpv_args"]})
        self.assertEqual(len(self.app.workers), n)

    async def test_language_switch_translates(self):
        self.app.apply_changes({"language": "en"})
        self.assertEqual(self.app.lang, "en")
        self.assertEqual(self.app.widgets["#open_library_btn"].label, "📁 Library")
        self.assertEqual(self.app.widgets["#ctl_toggle"].tooltip, "Play / pause (Space)")

    async def test_mpv_death_is_handled(self):
        await self.play_from(0)
        self.app.player._proc.kill()
        self.assertTrue(await self.app.settle(lambda: "mpv" in self.log() and not self.app.player.running))
        self.app.log_box.lines.clear()
        await self.app.action_next()
        self.assertTrue(self.log())                                    # пользователь видит причину
        await self.app.settle()
        self.assertIn("mpv", self.app.widgets["#now_title"].text)
        await self.app.action_restart_player()                         # восстановление из палитры
        self.assertTrue(self.app.player.running)

    async def test_quit_saves_state_and_restore_works(self):
        await self.play_from(2)
        self.assertTrue(await self.app.settle(lambda: self.app.player.props.get("volume") == 80.0))
        await self.app.player.set_property("volume", 61)
        await self.app.settle(lambda: self.app.player.props.get("volume") == 61)
        await self.app.action_quit()
        self.assertTrue(self.app.exited)
        self.assertFalse(self.app.player.running)
        with open(queue_store.paths.QUEUE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved, {"files": self.files, "index": 2})
        with open(queue_store.paths.SETTINGS_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["volume"], 61)

        app2 = AppUnderTest()
        app2.settings["mpv_path"] = self.binary
        try:
            self.assertTrue(await app2._start_player())
            await app2._restore_queue()
            self.assertTrue(await app2.settle(lambda: len(app2.widgets["#queue"].options) == 5))
            self.assertEqual(app2._resume_index, 2)
            self.assertTrue(app2.player.props.get("idle-active"))      # восстановили, но не стартуем сами
        finally:
            await app2.player.stop()

    async def test_cli_targets(self):
        sub = os.path.join(self.music, "x")
        os.makedirs(sub)
        open(os.path.join(sub, "z.ogg"), "w").close()
        app = AppUnderTest(targets=[sub, self.files[0], "https://a.b/c.mp3", "/missing/file.mp3"])
        app.settings["mpv_path"] = self.binary
        try:
            await app._start_player()
            await app._load_cli_targets()
            self.assertTrue(await app.settle(lambda: len(app.widgets["#queue"].options) == 3))
            self.assertIn("/missing/file.mp3", app.log_box.joined())
        finally:
            await app.player.stop()

    async def test_end_file_error_logged(self):
        await self.app.player.command("fake_end_error")
        self.assertTrue(await self.app.settle(lambda: "loading failed" in self.log()))

    async def test_paths_and_music_dir_validation(self):
        self.app._apply_paths({"music_dir": "/definitely/missing", "mpv_args": ""})
        self.assertIn("/definitely/missing", self.log())
        self.assertEqual(self.app.settings["music_dir"], self.music)   # неверная папка не принята
        self.app._apply_music_dir(self.tmp)
        self.assertEqual(self.app.settings["music_dir"], self.tmp)

    async def test_bindings_and_controls_consistent(self):
        keys = {b.action for b in make_bindings("en")}
        for _cid, _label, action, _tip in CONTROLS:
            self.assertTrue(hasattr(self.app, "action_" + action.split("(")[0]), action)
        self.assertEqual(len(CONTROL_ACTIONS), len(CONTROLS))
        for b in make_bindings("en"):                                  # у каждой привязки есть обработчик
            self.assertTrue(hasattr(self.app, "action_" + b.action.split("(")[0]), b.action)
        self.assertIn("toggle_pause", keys)


if __name__ == "__main__":
    unittest.main()
