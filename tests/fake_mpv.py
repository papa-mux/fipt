#!/usr/bin/env python3
"""Фейковый mpv для тестов: понимает подмножество JSON IPC (observe/get/set/loadfile/stop/quit)."""
import asyncio
import json
import os
import sys

sock_path = None
volume = 100.0
for a in sys.argv[1:]:
    if a.startswith("--input-ipc-server="):
        sock_path = a.split("=", 1)[1]
    elif a.startswith("--volume="):
        volume = float(a.split("=", 1)[1])
if "--crash" in sys.argv:
    sys.stderr.write("fake mpv: bad option\n")
    sys.exit(2)

props = {"pause": False, "volume": volume, "mute": False, "speed": 1.0, "time-pos": None,
         "duration": None, "idle-active": True, "playlist": [], "playlist-pos": -1,
         "loop-file": False, "loop-playlist": False, "media-title": None, "metadata": None,
         "track-list": [], "path": None, "last-seek": None, "aid": "auto", "sid": "auto", "vid": "auto", "replaygain": "no"}
observers = []  # (writer, id, name)


def send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())


def notify(name):
    for w, oid, n in list(observers):
        if n == name:
            ev = {"event": "property-change", "id": oid, "name": name}
            if props[name] is not None:
                ev["data"] = props[name]
            try:
                send(w, ev)
            except Exception:
                pass


def sync_playlist():
    pos = props["playlist-pos"]
    props["playlist"] = [
        {"id": i + 1, "filename": f, "current": i == pos, "playing": i == pos}
        for i, f in enumerate(files)
    ]
    props["idle-active"] = pos < 0
    props["media-title"] = files[pos].rsplit("/", 1)[-1] if pos >= 0 else None
    props["path"] = files[pos] if pos >= 0 else None
    props["duration"] = 120.0 if pos >= 0 else None
    props["time-pos"] = 0.0 if pos >= 0 else None
    for n in ("playlist", "playlist-pos", "idle-active", "media-title", "path", "duration", "time-pos"):
        notify(n)


files = []
backup = []


async def handle(reader, writer):
    while True:
        line = await reader.readline()
        if not line:
            break
        msg = json.loads(line)
        cmd, rid = msg["command"], msg.get("request_id")

        def ok(data=None):
            r = {"error": "success", "request_id": rid}
            if data is not None:
                r["data"] = data
            send(writer, r)

        def fail(e):
            send(writer, {"error": e, "request_id": rid})

        name = cmd[0]
        if name == "observe_property":
            observers.append((writer, cmd[1], cmd[2]))
            ok()
            if cmd[2] in props:
                notify(cmd[2])
        elif name == "get_property":
            v = props.get(cmd[1])
            ok(v) if v is not None else fail("property unavailable")
        elif name == "set_property":
            if cmd[1] not in props:
                fail("property not found")
            else:
                props[cmd[1]] = cmd[2]
                ok()
                notify(cmd[1])
                if cmd[1] == "playlist-pos":
                    sync_playlist()
        elif name == "loadfile":
            files.append(cmd[1])
            if len(cmd) > 2 and cmd[2] == "append-play" and props["playlist-pos"] < 0:
                props["playlist-pos"] = len(files) - 1
            ok()
            sync_playlist()
        elif name == "stop":
            if "keep-playlist" not in cmd[1:]:
                files.clear()
            props["playlist-pos"] = -1
            if "idle-active" in props:
                props["idle-active"] = True
            ok()
            sync_playlist()
        elif name in ("playlist-next", "playlist-prev"):
            weak = len(cmd) > 1 and cmd[1] == "weak"
            step = 1 if name == "playlist-next" else -1
            target = props["playlist-pos"] + step
            if 0 <= target < len(files):
                props["playlist-pos"] = target
                ok()
                sync_playlist()
            elif weak:
                ok()
            else:
                fail("error running command")
        elif name == "playlist-remove":
            idx = cmd[1]
            if not isinstance(idx, int) or not 0 <= idx < len(files):
                fail("invalid parameter")
            else:
                files.pop(idx)
                pos = props["playlist-pos"]
                if idx < pos:
                    pos -= 1
                elif idx == pos:
                    pos = min(pos, len(files) - 1)
                props["playlist-pos"] = pos
                ok()
                sync_playlist()
        elif name == "playlist-move":
            a, b = cmd[1], cmd[2]
            if not (0 <= a < len(files) and 0 <= b <= len(files)):
                fail("invalid parameter")
            else:
                cur = files[props["playlist-pos"]] if props["playlist-pos"] >= 0 else None
                item = files.pop(a)
                files.insert(b if b <= a else b - 1, item)   # «a встаёт перед b»
                props["playlist-pos"] = files.index(cur) if cur is not None else -1
                ok()
                sync_playlist()
        elif name == "playlist-shuffle":
            cur = files[props["playlist-pos"]] if props["playlist-pos"] >= 0 else None
            backup[:] = files                                  # чтобы playlist-unshuffle мог вернуть порядок
            files.reverse()                                    # детерминированный «шафл»
            props["playlist-pos"] = files.index(cur) if cur is not None else -1
            ok()
            sync_playlist()
        elif name == "playlist-unshuffle":
            if not backup:
                fail("error running command")
            else:
                cur = files[props["playlist-pos"]] if props["playlist-pos"] >= 0 else None
                files[:] = [f for f in backup if f in files]
                backup.clear()
                props["playlist-pos"] = files.index(cur) if cur is not None else -1
                ok()
                sync_playlist()
        elif name == "cycle" and cmd[1] in ("pause", "mute"):
            props[cmd[1]] = not props[cmd[1]]
            ok()
            notify(cmd[1])
        elif name == "add" and cmd[1] in ("volume", "speed"):
            props[cmd[1]] = round(props[cmd[1]] + cmd[2], 3)
            ok()
            notify(cmd[1])
        elif name == "seek":
            props["last-seek"] = [cmd[1], cmd[2] if len(cmd) > 2 else "relative"]
            ok()
        elif name == "fake_end_error":
            ok()
            send(writer, {"event": "end-file", "reason": "error", "file_error": "loading failed"})
        elif name == "quit":
            ok()
            await writer.drain()
            os._exit(0)
        else:
            fail("invalid parameter")
        await writer.drain()


async def main():
    server = await asyncio.start_unix_server(handle, sock_path)
    async with server:
        await server.serve_forever()


try:
    asyncio.run(main())
except (RuntimeError, KeyboardInterrupt):
    pass
