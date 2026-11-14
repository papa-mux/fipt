"""Тесты D-Bus-слоя и MPRIS. Запуск: python3 tests/test_mpris.py

Логика (PlayerView, метаданные, маршалинг) проверяется без шины. Интеграционная часть поднимает
изолированный dbus-daemon (если он есть в системе; иначе эти тесты пропускаются) и ходит к сервису
вторым соединением — так же, как ходили бы playerctl или рабочий стол.
"""
import asyncio
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from fiptui import dbus_min as D  # noqa: E402
from fiptui import mpris as M  # noqa: E402
from fiptui.dbus_min import DBusConnection, DBusError, Variant  # noqa: E402

IFACE = M.IFACE_PLAYER
PROPS = "org.freedesktop.DBus.Properties"


def make_props(**over):
    props = {"idle-active": False, "pause": False, "time-pos": 12.5, "duration": 200.0, "volume": 80,
             "speed": 1.0, "media-title": "Song", "metadata": {"artist": "Artist", "ALBUM": "Alb"},
             "path": "/music/My Song #1.mp3", "playlist-pos": 1, "loop-file": "no", "loop-playlist": "no"}
    props.update(over)
    return props


ENTRIES = [{"id": 5, "filename": "/a.mp3"}, {"id": 6, "filename": "/music/My Song #1.mp3", "current": True},
           {"id": 7, "filename": "/c.mp3"}]


def view(shuffled=False, running=True, entries=None, **over):
    return M.PlayerView.from_props(make_props(**over), ENTRIES if entries is None else entries, shuffled, running)


# ───────────────────────── маршалинг ─────────────────────────

class MarshalTests(unittest.TestCase):
    def roundtrip(self, sig, values, endian="<"):
        self.assertEqual(D.unmarshal(sig, D.marshal(sig, values, endian), endian), list(values))

    def test_basic_types(self):
        self.roundtrip("ybnqiuxtd", (7, True, -3, 4, -5, 6, -2 ** 40, 2 ** 50, 1.25))
        self.roundtrip("sog", ("Привет, мир", "/a/b", "a{sv}"))

    def test_big_endian(self):
        self.roundtrip("usx", (1, "é", 99), ">")

    def test_containers(self):
        self.roundtrip("as", (["a", "б"],))
        self.roundtrip("a{sv}", ({"k": Variant("s", "v"), "n": Variant("x", 5), "l": Variant("as", ["q"])},))
        self.roundtrip("a{sv}", ({},))
        self.roundtrip("(yu)", ((1, 2),))
        self.roundtrip("a(ys)", ([(1, "a"), (2, "bb")],))
        self.roundtrip("ay", ([1, 2, 3],))

    def test_empty_array_of_8_aligned_elements_is_padded(self):
        # пустой массив структур/словарей всё равно выравнивается по границе элемента
        self.assertEqual(len(D.marshal("ia{sv}", (1, {}))), 8)

    def test_errors(self):
        with self.assertRaises(DBusError):
            D.marshal("y", ())
        with self.assertRaises(DBusError):
            D.marshal("s", ("a\0b",))
        with self.assertRaises(DBusError):
            D.marshal("n", (70000,))
        with self.assertRaises(ValueError):
            D.unmarshal("s", b"\x05\0\0\0ab")           # обрезано
        with self.assertRaises(ValueError):
            D.split_signature("a{s")

    def test_message_roundtrip(self):
        msg = D.Message(D.SIGNAL, 9, path="/x", interface="a.b", member="M", signature="sa{sv}as",
                        body=("i", {"k": Variant("y", 3)}, ["z"]))
        raw = msg.to_bytes()
        import struct
        _, _, fields_len = struct.unpack("<III", raw[4:16])
        head_len = 16 + fields_len
        head_len += -head_len % 8
        back = D.Message.parse(raw[:head_len], raw[head_len:])
        self.assertEqual((back.type, back.serial, back.path, back.interface, back.member, back.signature, back.body),
                         (D.SIGNAL, 9, "/x", "a.b", "M", "sa{sv}as", ["i", {"k": Variant("y", 3)}, ["z"]]))

    def test_bus_addresses(self):
        env = {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=/tmp/dbus-x%20y,guid=1;unix:path=/run/u/1/bus"}
        self.assertEqual(D.session_bus_paths(env, 1000),
                         ["\0/tmp/dbus-x y", "/run/u/1/bus", "/run/user/1000/bus"])
        self.assertEqual(D.session_bus_paths({"XDG_RUNTIME_DIR": "/r"}, 5), ["/r/bus"])

    def test_introspection_is_wellformed_xml(self):
        import xml.dom.minidom
        service = M.MprisService(FakeHost())
        conn = DBusConnection()
        conn.export(M.OBJECT_PATH, service.root, service.player)
        xml.dom.minidom.parseString(conn._introspect(M.OBJECT_PATH))
        root_xml = conn._introspect("/org")
        self.assertIn('<node name="mpris"/>', root_xml)


# ───────────────────────── состояние плеера ─────────────────────────

class PlayerViewTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(view().status, "Playing")
        self.assertEqual(view(pause=True).status, "Paused")
        self.assertEqual(view(**{"idle-active": True}).status, "Stopped")
        self.assertEqual(view(running=False).status, "Stopped")

    def test_metadata(self):
        meta = M.build_metadata(view())
        self.assertEqual(meta["mpris:trackid"], Variant("o", "/org/fipt/Track/6"))
        self.assertEqual(meta["mpris:length"], Variant("x", 200_000_000))
        self.assertEqual(meta["xesam:title"], Variant("s", "Song"))
        self.assertEqual(meta["xesam:artist"], Variant("as", ["Artist"]))
        self.assertEqual(meta["xesam:album"], Variant("s", "Alb"))
        self.assertEqual(meta["xesam:url"], Variant("s", "file:///music/My%20Song%20%231.mp3"))

    def test_metadata_without_tags_or_duration(self):
        meta = M.build_metadata(view(duration=None, metadata=None, **{"media-title": "stream"}))
        self.assertNotIn("mpris:length", meta)
        self.assertNotIn("xesam:artist", meta)
        self.assertEqual(meta["xesam:title"], Variant("s", "stream"))

    def test_metadata_when_stopped_has_no_track(self):
        meta = M.build_metadata(view(**{"idle-active": True}))
        self.assertEqual(meta, {"mpris:trackid": Variant("o", M.NO_TRACK)})

    def test_url_track_keeps_url(self):
        self.assertEqual(M.path_to_uri("https://x.org/a b.mp3"), "https://x.org/a b.mp3")

    def test_position_and_length(self):
        v = view()
        self.assertEqual((v.position_us, v.length_us), (12_500_000, 200_000_000))
        self.assertEqual(view(**{"time-pos": None}).position_us, 0)
        self.assertIsNone(view(duration=0).length_us)

    def test_capabilities(self):
        self.assertTrue(view().can_next and view().can_previous and view().can_seek)
        last = view(**{"playlist-pos": 2})
        self.assertFalse(last.can_next)
        self.assertTrue(view(**{"playlist-pos": 2, "loop-playlist": "inf"}).can_next)
        first = view(**{"playlist-pos": 0})
        self.assertFalse(first.can_previous)
        self.assertTrue(view(**{"playlist-pos": 0, "loop-playlist": "inf"}).can_previous)
        self.assertFalse(view(duration=None).can_seek)
        idle = view(**{"idle-active": True})
        self.assertTrue(idle.can_play)
        self.assertFalse(idle.can_pause or idle.can_next or idle.can_seek)
        self.assertFalse(view(running=False).can_play)
        self.assertFalse(view(entries=[], **{"idle-active": True, "playlist-pos": -1}).can_play)

    def test_repeat_shuffle_rate_volume(self):
        v = view(shuffled=True, **{"loop-file": "inf", "speed": 2.0, "volume": 130})
        props = M.player_properties(v)
        self.assertEqual(props["LoopStatus"].value, "Track")
        self.assertTrue(props["Shuffle"].value)
        self.assertEqual(props["Rate"].value, 2.0)
        self.assertAlmostEqual(props["Volume"].value, 1.3)
        self.assertEqual(M.player_properties(view(**{"loop-playlist": "inf"}))["LoopStatus"].value, "Playlist")
        self.assertEqual(M.player_properties(view(speed=100.0))["Rate"].value, M.MAX_RATE)   # в пределах Min/Max

    def test_current_entry_found_without_playlist_pos(self):
        self.assertEqual(view(**{"playlist-pos": None}).entry_id, 6)

    def test_uri_to_target(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", prefix="my song ") as f:
            self.assertEqual(M.uri_to_target(M.path_to_uri(f.name)), f.name)
        self.assertEqual(M.uri_to_target("https://x.org/a.mp3"), "https://x.org/a.mp3")
        for bad in ("ftp://x/y", "file:///definitely/missing.mp3", "file://otherhost/etc/passwd", "garbage", "http://"):
            with self.assertRaises(DBusError, msg=bad):
                M.uri_to_target(bad)


# ───────────────────────── интеграция с настоящей шиной ─────────────────────────

class FakeHost:
    def __init__(self):
        self.props = make_props()
        self.entries = copy.deepcopy(ENTRIES)
        self.shuffled = False
        self.calls = []

    def mpris_view(self):
        return M.PlayerView.from_props(self.props, self.entries, self.shuffled, True)

    def __getattr__(self, name):
        if not name.startswith("mpris_"):
            raise AttributeError(name)

        async def record(*args):
            self.calls.append((name[6:], args))
        return record


def start_daemon(tmp):
    exe = shutil.which("dbus-daemon")
    if not exe:
        return None, None
    sock = os.path.join(tmp, "bus.sock")
    conf = os.path.join(tmp, "bus.conf")
    with open(conf, "w") as f:
        f.write(f'''<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:path={sock}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/>
<allow send_type="signal"/><allow receive_type="signal"/><allow own="*"/></policy></busconfig>''')
    proc = subprocess.Popen([exe, f"--config-file={conf}", "--nofork", "--print-address"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline()
    if not line.startswith("unix:"):
        proc.kill()
        return None, None
    return proc, sock


class BusTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.daemon, cls.sock = start_daemon(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        if cls.daemon:
            cls.daemon.kill()
            cls.daemon.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    async def asyncSetUp(self):
        if not self.daemon:
            self.skipTest("dbus-daemon недоступен")
        self.host = FakeHost()
        self.errors = []
        self.service = M.MprisService(self.host, on_error=self.errors.append, paths=[self.sock])
        self.assertTrue(await self.service.start())
        self.client = DBusConnection()
        await self.client.connect([self.sock])

    async def asyncTearDown(self):
        if self.daemon:
            await self.client.close()
            await self.service.stop()

    async def call(self, member, sig="", body=(), iface=IFACE, dest=M.BUS_NAME):
        return await self.client.call(dest, M.OBJECT_PATH, iface, member, sig, body)

    async def get(self, name, iface=IFACE):
        return (await self.call("Get", "ss", (iface, name), PROPS))[0]

    async def settle(self):
        await asyncio.sleep(0.15)

    async def test_get_all_matches_view(self):
        (props,) = await self.call("GetAll", "s", (IFACE,), PROPS)
        self.assertEqual(props["PlaybackStatus"], Variant("s", "Playing"))
        self.assertEqual(props["Volume"], Variant("d", 0.8))
        self.assertEqual(props["Position"], Variant("x", 12_500_000))
        self.assertEqual(props["Metadata"].value["xesam:title"], Variant("s", "Song"))
        self.assertEqual(set(props), set(M.player_properties(view())) | {"Position"})

    async def test_root_interface(self):
        (props,) = await self.call("GetAll", "s", (M.IFACE_ROOT,), PROPS)
        self.assertEqual(props["Identity"].value, "FIPT")
        self.assertTrue(props["CanQuit"].value)
        self.assertFalse(props["HasTrackList"].value)
        self.assertIn("file", props["SupportedUriSchemes"].value)

    async def test_methods_reach_host(self):
        for name in ("Play", "Pause", "PlayPause", "Stop", "Next", "Previous"):
            await self.call(name)
        await self.call("Seek", "x", (5_000_000,))
        await self.call("Seek", "x", (-2_000_000,))
        self.assertEqual([c[0] for c in self.host.calls],
                         ["play", "pause", "play_pause", "stop", "next", "previous", "seek", "seek"])
        self.assertEqual(self.host.calls[6][1], (5.0, False))
        self.assertEqual(self.host.calls[7][1], (-2.0, False))

    async def test_set_position_checks_track_id(self):
        await self.call("SetPosition", "ox", ("/org/fipt/Track/6", 30_000_000))
        await self.call("SetPosition", "ox", ("/org/fipt/Track/99", 30_000_000))      # чужой трек
        await self.call("SetPosition", "ox", ("/org/fipt/Track/6", 999_000_000))      # за пределами
        await self.call("SetPosition", "ox", ("/org/fipt/Track/6", -1))
        self.assertEqual(self.host.calls, [("seek", (30.0, True))])

    async def test_open_uri(self):
        await self.call("OpenUri", "s", ("https://x.org/a.mp3",))
        self.assertEqual(self.host.calls, [("open", ("https://x.org/a.mp3",))])
        with self.assertRaises(DBusError) as ctx:
            await self.call("OpenUri", "s", ("ftp://x/y",))
        self.assertEqual(ctx.exception.name, D.ERR_INVALID_ARGS)

    async def test_writable_properties(self):
        for name, sig, value in (("Volume", "d", 0.3), ("LoopStatus", "s", "Playlist"), ("Shuffle", "b", True),
                                 ("Rate", "d", 1.5)):
            await self.call("Set", "ssv", (IFACE, name, Variant(sig, value)), PROPS)
        await self.settle()
        calls = {c[0]: c[1] for c in self.host.calls}
        self.assertAlmostEqual(calls["set_volume"][0], 30.0)
        self.assertEqual(calls["set_repeat"], ("playlist",))
        self.assertEqual(calls["set_shuffle"], (True,))
        self.assertEqual(calls["set_rate"], (1.5,))

    async def test_bad_writes(self):
        cases = [
            (("LoopStatus", "s", "Bogus"), D.ERR_INVALID_ARGS),
            (("Rate", "d", 0.0), D.ERR_INVALID_ARGS),
            (("Volume", "s", "loud"), D.ERR_INVALID_ARGS),             # неверный тип
            (("PlaybackStatus", "s", "Playing"), D.ERR_PROP_READONLY),
            (("Nope", "s", "x"), D.ERR_UNKNOWN_PROP),
        ]
        for (name, sig, value), expected in cases:
            with self.assertRaises(DBusError, msg=name) as ctx:
                await self.call("Set", "ssv", (IFACE, name, Variant(sig, value)), PROPS)
            self.assertEqual(ctx.exception.name, expected, name)
        self.assertEqual(self.host.calls, [])

    async def test_errors(self):
        with self.assertRaises(DBusError) as ctx:
            await self.call("Bogus")
        self.assertEqual(ctx.exception.name, D.ERR_UNKNOWN_METHOD)
        with self.assertRaises(DBusError) as ctx:
            await self.call("Seek", "s", ("x",))                          # неверная сигнатура
        self.assertEqual(ctx.exception.name, D.ERR_INVALID_ARGS)
        with self.assertRaises(DBusError) as ctx:
            await self.client.call(M.BUS_NAME, "/nope", IFACE, "Play")
        self.assertEqual(ctx.exception.name, D.ERR_UNKNOWN_OBJECT)
        with self.assertRaises(DBusError) as ctx:
            await self.call("GetAll", "s", ("no.such.Iface",), PROPS)
        self.assertEqual(ctx.exception.name, D.ERR_UNKNOWN_IFACE)

    async def test_peer_and_introspect(self):
        await self.call("Ping", iface="org.freedesktop.DBus.Peer")
        (xml,) = await self.call("Introspect", iface="org.freedesktop.DBus.Introspectable")
        for needle in ("org.mpris.MediaPlayer2.Player", 'name="SetPosition"', 'name="Seeked"', "Metadata"):
            self.assertIn(needle, xml)

    async def collect_signals(self, until):
        got = []
        self.client.signal_handlers.append(got.append)
        await self.client.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "AddMatch", "s", (f"type='signal',path='{M.OBJECT_PATH}'",))
        await until()
        await self.settle()
        return got

    async def test_properties_changed_only_for_real_changes(self):
        async def mutate():
            self.host.props["pause"] = True
            self.service.on_property("pause")
            self.host.props["volume"] = 50
            self.service.on_property("volume")
            self.service.on_property("time-pos")                       # не наблюдаемое — ничего не шлёт
            self.service.on_property("pause")                          # повторно: изменений нет
        got = await self.collect_signals(mutate)
        changes = [m for m in got if m.member == "PropertiesChanged"]
        self.assertEqual(len(changes), 1, "пачка изменений должна склеиться в один сигнал")
        iface, changed, invalidated = changes[0].body
        self.assertEqual(iface, IFACE)
        self.assertEqual(changed["PlaybackStatus"], Variant("s", "Paused"))
        self.assertEqual(changed["Volume"], Variant("d", 0.5))
        self.assertEqual(set(changed), {"PlaybackStatus", "Volume"})
        self.assertEqual(invalidated, [])

    async def test_track_change_updates_metadata_and_capabilities(self):
        async def mutate():
            self.host.props.update({"playlist-pos": 2, "media-title": "Other", "metadata": {}, "duration": 10.0})
            self.host.entries[1].pop("current")
            self.host.entries[2]["current"] = True
            self.service.on_property("playlist-pos")
        got = await self.collect_signals(mutate)
        changed = [m for m in got if m.member == "PropertiesChanged"][0].body[1]
        self.assertEqual(changed["Metadata"].value["mpris:trackid"], Variant("o", "/org/fipt/Track/7"))
        self.assertEqual(changed["CanGoNext"], Variant("b", False))
        self.assertNotIn("PlaybackStatus", changed)

    async def test_shuffle_change_is_published(self):
        async def mutate():
            self.host.shuffled = True
            self.service.schedule_flush()
        got = await self.collect_signals(mutate)
        self.assertEqual([m.body[1] for m in got if m.member == "PropertiesChanged"], [{"Shuffle": Variant("b", True)}])

    async def test_seeked_after_seek_and_restart(self):
        async def mutate():
            self.host.props["time-pos"] = 90.0
            self.service.on_event({"event": "playback-restart"})       # просто начало трека: Seeked не нужен
            self.service.on_event({"event": "seek"})
            self.service.on_event({"event": "playback-restart"})
        got = await self.collect_signals(mutate)
        seeked = [m for m in got if m.member == "Seeked"]
        self.assertEqual([m.body for m in seeked], [[90_000_000]])

    async def test_quit_replies_then_quits(self):
        await self.call("Quit", iface=M.IFACE_ROOT)
        await self.settle()
        self.assertEqual(self.host.calls, [("quit", ())])

    async def test_second_instance_gets_own_name_and_stop_releases(self):
        other = M.MprisService(FakeHost(), paths=[self.sock])
        self.assertTrue(await other.start())
        try:
            self.assertEqual(other.owned_name, f"{M.BUS_NAME}.instance{os.getpid()}")
            (props,) = await self.client.call(other.owned_name, M.OBJECT_PATH, PROPS, "GetAll", "s", (M.IFACE_ROOT,))
            self.assertEqual(props["Identity"].value, "FIPT")
        finally:
            await other.stop()
        await self.service.stop()
        (names,) = await self.client.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          "ListNames")
        self.assertFalse([n for n in names if n.startswith("org.mpris.MediaPlayer2.fipt")])
        self.assertTrue(await self.service.start())                    # после stop можно стартовать снова

    async def test_service_survives_garbage_and_client_disconnect(self):
        extra = DBusConnection()
        await extra.connect([self.sock])
        await extra.call(M.BUS_NAME, M.OBJECT_PATH, IFACE, "Play")
        await extra.close()
        await self.call("Play")
        self.assertEqual([c[0] for c in self.host.calls], ["play", "play"])

    async def test_host_exception_becomes_dbus_error(self):
        async def boom():
            raise RuntimeError("mpv exploded")
        self.host.mpris_play = boom
        self.service.player.methods["Play"] = ("", "", boom)
        with self.assertRaises(DBusError) as ctx:
            await self.call("Play")
        self.assertEqual(ctx.exception.name, D.ERR_FAILED)
        self.assertIn("mpv exploded", ctx.exception.message)
        await self.call("Pause")                                       # сервис жив


class NoBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_returns_false_without_bus(self):
        errors = []
        service = M.MprisService(FakeHost(), on_error=errors.append, paths=["/definitely/no/bus"])
        self.assertFalse(await service.start())
        self.assertFalse(service.running)
        self.assertTrue(errors and errors[0].startswith("MPRIS"))
        await service.stop()                                           # stop без start не падает
        service.on_property("pause")                                   # уведомления без шины — тихо игнорируются
        service.schedule_flush()


if __name__ == "__main__":
    unittest.main()
