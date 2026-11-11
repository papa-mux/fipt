"""Тесты IPC-слоя на фейковом mpv (tests/fake_mpv.py). Запуск: python3 tests/test_mpv_ipc.py"""
import asyncio
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from fiptui import mpv as M  # noqa: E402


def make_settings(binary: str, **extra) -> dict:
    s = {"mpv_path": binary, "volume": 55, "replaygain": "no", "ytdl": True, "video": "window", "mpv_args": ""}
    s.update(extra)
    return s


class PureTests(unittest.TestCase):
    def test_build_args(self):
        a = M.build_args(make_settings("mpv", video="off", mpv_args="--cache=yes --x='a b'"), "/tmp/s.sock")
        self.assertIn("--idle=yes", a)
        self.assertIn("--input-ipc-server=/tmp/s.sock", a)
        self.assertIn("--volume=55", a)
        self.assertIn("--no-video", a)
        self.assertEqual(a[-2:], ["--cache=yes", "--x=a b"])  # пользовательские аргументы — последними

    def test_bad_extra_args_ignored(self):
        self.assertEqual(M.split_extra_args("--a 'unclosed"), [])

    def test_terminal_args(self):
        a = M.terminal_args(make_settings("mpv", terminal_vo="kitty"), ["/m/a.mp3", "-weird.mp3"], 1)
        self.assertEqual(a[0], "--vo=kitty")
        self.assertIn("--playlist-start=1", a)
        self.assertEqual(a[a.index("--") + 1:], ["/m/a.mp3", "-weird.mp3"])  # имена с «-» защищены

    def test_repeat_cycle(self):
        self.assertEqual(M.repeat_mode(False, False), "off")
        self.assertEqual(M.repeat_mode("inf", "no"), "track")
        self.assertEqual(M.repeat_mode("no", "inf"), "playlist")
        mode, seen = "off", []
        for _ in range(3):
            mode = M.NEXT_REPEAT[mode]
            seen.append(mode)
        self.assertEqual(seen, ["track", "playlist", "off"])
        self.assertEqual(dict(M.repeat_properties("track"))["loop-file"], "inf")

    def test_tracks(self):
        tl = [{"type": "audio", "id": 1, "lang": "eng", "codec": "aac"}, {"type": "sub", "id": 1}]
        self.assertEqual(len(M.tracks_of(tl, "audio")), 1)
        self.assertEqual(M.tracks_of(None, "audio"), [])
        self.assertEqual(M.describe_track(tl[0]), "#1 · eng · aac")
        self.assertNotIn("[", M.describe_track({"id": 2, "title": "x", "demux-channel-count": 6}))

    def test_end_file(self):
        self.assertIsNone(M.describe_end_file({"event": "end-file", "reason": "eof"}))
        key, detail = M.describe_end_file({"event": "end-file", "reason": "error", "file_error": "loading failed"})
        self.assertEqual((key, detail), ("log_err_open", "loading failed"))
        key, _ = M.describe_end_file({"event": "end-file", "reason": "error", "file_error": "unrecognized file format"})
        self.assertEqual(key, "log_err_format")

    def test_no_binary(self):
        self.assertIsNone(M.resolve_binary({"mpv_path": "definitely-not-mpv-xyz"}))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.binary = os.path.join(self.tmp, "mpv")
        with open(self.binary, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'fake_mpv.py')} \"$@\"\n")
        os.chmod(self.binary, os.stat(self.binary).st_mode | stat.S_IEXEC)
        self.props, self.events, self.exited = {}, [], []
        self.client = M.MpvClient(
            on_property=lambda n, v: self.props.__setitem__(n, v),
            on_event=self.events.append,
            on_exit=lambda: self.exited.append(True),
        )

    async def asyncTearDown(self):
        await self.client.stop()

    async def wait_for(self, cond, timeout=3.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False

    async def test_start_observe_and_stop(self):
        self.assertIsNone(await self.client.start(make_settings(self.binary)))
        self.assertTrue(self.client.running)
        sock = self.client.socket_path
        self.assertTrue(await self.wait_for(lambda: self.props.get("volume") == 55.0))  # --volume дошёл
        self.assertIs(self.props.get("pause"), False)
        await self.client.stop()
        self.assertFalse(self.client.running)
        self.assertFalse(os.path.exists(sock))          # временная папка с сокетом убрана
        self.assertEqual(self.exited, [])               # штатная остановка — не «падение»

    async def test_no_mpv(self):
        self.assertEqual(await self.client.start(make_settings("no-such-mpv-binary")), "no_mpv")

    async def test_mpv_crash_reports_exit(self):
        err = await self.client.start(make_settings(self.binary, mpv_args="--crash"))
        self.assertTrue(err and err.startswith("exited:2"), err)
        self.assertIn("bad option", err)

    async def test_commands_and_errors(self):
        await self.client.start(make_settings(self.binary))
        await self.client.set_property("volume", 70)
        self.assertEqual(await self.client.get_property("volume"), 70)
        with self.assertRaises(M.MpvError):
            await self.client.get_property("time-pos")   # недоступно до начала воспроизведения
        with self.assertRaises(M.MpvError):
            await self.client.command("no-such-command")

    async def test_replace_queue_order_and_start(self):
        await self.client.start(make_settings(self.binary))
        files = [f"/music/{i}.mp3" for i in range(300)]    # много команд подряд
        await self.client.replace_queue(files, 42)
        self.assertTrue(await self.wait_for(lambda: self.props.get("playlist-pos") == 42))
        names = [e["filename"] for e in self.props["playlist"]]
        self.assertEqual(names, files)                    # порядок сохранён
        cur = [e for e in self.props["playlist"] if e["current"]]
        self.assertEqual(cur[0]["filename"], "/music/42.mp3")
        self.assertIs(self.props.get("idle-active"), False)

    async def test_append_starts_when_idle(self):
        await self.client.start(make_settings(self.binary))
        self.assertTrue(await self.wait_for(lambda: self.client.props.get("idle-active") is True))
        await self.client.append(["/m/a.mp3", "/m/b.mp3"])
        self.assertTrue(await self.wait_for(lambda: self.props.get("playlist-pos") == 0))
        await self.client.append(["/m/c.mp3"])            # плеер занят — не перебивает
        self.assertTrue(await self.wait_for(lambda: len(self.props.get("playlist", [])) == 3))
        self.assertEqual(self.props["playlist-pos"], 0)

    async def test_large_playlist_line(self):
        await self.client.start(make_settings(self.binary))
        files = ["/very/long/path/" + "x" * 200 + f"{i}.mp3" for i in range(1000)]  # строка > 64 КБ
        await self.client.replace_queue(files, 0)
        self.assertTrue(await self.wait_for(lambda: len(self.props.get("playlist", [])) == 1000))

    async def test_events_forwarded(self):
        await self.client.start(make_settings(self.binary))
        await self.client.command("fake_end_error")
        self.assertTrue(await self.wait_for(lambda: any(e.get("event") == "end-file" for e in self.events)))

    async def test_external_death_calls_on_exit(self):
        await self.client.start(make_settings(self.binary))
        self.client._proc.kill()                           # mpv убили снаружи
        self.assertTrue(await self.wait_for(lambda: self.exited == [True]))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with self.assertRaises(M.MpvError):
            await self.client.command("stop")
        self.assertLess(loop.time() - t0, 1.0)             # отказ мгновенный, а не таймаут в 5 секунд

    async def test_restart_after_stop(self):
        self.assertIsNone(await self.client.start(make_settings(self.binary)))
        await self.client.stop()
        self.assertIsNone(await self.client.start(make_settings(self.binary)))
        self.assertTrue(self.client.running)


if __name__ == "__main__":
    unittest.main()
