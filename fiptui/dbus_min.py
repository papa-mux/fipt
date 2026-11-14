"""Минимальный клиент/сервер D-Bus на asyncio, без внешних зависимостей.

Нужен только для MPRIS (см. mpris.py): подключиться к сессионной шине, занять имя,
отвечать на вызовы методов / Properties / Introspect и рассылать сигналы.
Поддержаны типы: y b n q i u x t d s o g v a ( ) { } — этого хватает для MPRIS.
Файловые дескрипторы (h) не поддерживаются.

Спецификация: https://dbus.freedesktop.org/doc/dbus-specification.html
"""
from __future__ import annotations

import asyncio
import inspect
import os
import struct
from typing import Any, Callable
from urllib.parse import unquote_to_bytes
from xml.sax.saxutils import quoteattr

# типы сообщений и флаги
CALL, RETURN, ERROR, SIGNAL = 1, 2, 3, 4
NO_REPLY_EXPECTED = 0x1

# поля заголовка
F_PATH, F_INTERFACE, F_MEMBER, F_ERROR_NAME, F_REPLY_SERIAL = 1, 2, 3, 4, 5
F_DESTINATION, F_SENDER, F_SIGNATURE = 6, 7, 8

MAX_MESSAGE = 128 * 1024 * 1024
CALL_TIMEOUT = 10.0

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8,
          "s": 4, "o": 4, "g": 1, "v": 1, "a": 4, "(": 8, "{": 8}
_STRUCT = {"n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d"}


class DBusError(Exception):
    """Ошибка D-Bus: name — например 'org.freedesktop.DBus.Error.InvalidArgs'."""

    def __init__(self, name: str, message: str = "") -> None:
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message


class Variant:
    """Значение с явной сигнатурой: Variant('s', 'текст'), Variant('as', ['a', 'b'])."""
    __slots__ = ("signature", "value")

    def __init__(self, signature: str, value: Any) -> None:
        self.signature = signature
        self.value = value

    def __repr__(self) -> str:
        return f"Variant({self.signature!r}, {self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Variant) and (self.signature, self.value) == (other.signature, other.value)


# ───────────────────────── сигнатуры ─────────────────────────

def complete_type_end(sig: str, start: int = 0) -> int:
    """Индекс сразу после одного полного типа, начинающегося в sig[start]."""
    if start >= len(sig):
        raise ValueError("empty signature")
    c = sig[start]
    if c == "a":
        return complete_type_end(sig, start + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        pos = start + 1
        while pos < len(sig) and sig[pos] != close:
            pos = complete_type_end(sig, pos)
        if pos >= len(sig):
            raise ValueError(f"unbalanced signature {sig!r}")
        return pos + 1
    if c in _ALIGN or c == "h":
        return start + 1
    raise ValueError(f"bad type {c!r} in signature {sig!r}")


def split_signature(sig: str) -> list[str]:
    out, pos = [], 0
    while pos < len(sig):
        end = complete_type_end(sig, pos)
        out.append(sig[pos:end])
        pos = end
    return out


# ───────────────────────── упаковка ─────────────────────────

def _pad(buf: bytearray, n: int) -> None:
    buf.extend(b"\0" * (-len(buf) % n))


def _enc(buf: bytearray, t: str, v: Any, e: str) -> None:
    c = t[0]
    if c == "y":
        buf.append(int(v) & 0xFF)
    elif c == "b":
        _pad(buf, 4)
        buf += struct.pack(e + "I", 1 if v else 0)
    elif c in _STRUCT:
        _pad(buf, _ALIGN[c])
        if c == "d":
            v = float(v)
        else:
            v = int(v)
        try:
            buf += struct.pack(e + _STRUCT[c], v)
        except struct.error as exc:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", f"{v!r} does not fit '{c}'") from exc
    elif c in "so":
        data = str(v).encode("utf-8")
        if b"\0" in data:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", "NUL in string")
        _pad(buf, 4)
        buf += struct.pack(e + "I", len(data)) + data + b"\0"
    elif c == "g":
        data = str(v).encode("ascii")
        buf.append(len(data))
        buf += data + b"\0"
    elif c == "v":
        if not isinstance(v, Variant):
            raise TypeError(f"variant expected, got {v!r}")
        _enc(buf, "g", v.signature, e)
        _enc(buf, v.signature, v.value, e)
    elif c == "a":
        elem = t[1:]
        _pad(buf, 4)
        len_at = len(buf)
        buf += b"\0\0\0\0"
        _pad(buf, _ALIGN[elem[0]])
        start = len(buf)
        if elem[0] == "{":
            kt, vt = split_signature(elem[1:-1])
            for key, val in (v.items() if hasattr(v, "items") else v):
                _pad(buf, 8)
                _enc(buf, kt, key, e)
                _enc(buf, vt, val, e)
        else:
            for item in v:
                _enc(buf, elem, item, e)
        buf[len_at:len_at + 4] = struct.pack(e + "I", len(buf) - start)
    elif c == "(":
        _pad(buf, 8)
        parts = split_signature(t[1:-1])
        if len(parts) != len(v):
            raise TypeError(f"struct {t} needs {len(parts)} fields")
        for part, item in zip(parts, v):
            _enc(buf, part, item, e)
    else:
        raise ValueError(f"cannot marshal type {t!r}")


def marshal(signature: str, values: tuple | list, endian: str = "<") -> bytes:
    types = split_signature(signature)
    if len(types) != len(values):
        raise DBusError("org.freedesktop.DBus.Error.InvalidArgs",
                        f"signature {signature!r} needs {len(types)} values, got {len(values)}")
    buf = bytearray()
    for t, v in zip(types, values):
        _enc(buf, t, v, endian)
    return bytes(buf)


class _Reader:
    def __init__(self, data: bytes, endian: str) -> None:
        self.data, self.pos, self.e = data, 0, endian

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("message truncated")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def align(self, n: int) -> None:
        skip = -self.pos % n
        if skip:
            if any(self.take(skip)):
                raise ValueError("non-zero padding")


def _dec(r: _Reader, t: str) -> Any:
    c = t[0]
    if c == "y":
        return r.take(1)[0]
    if c == "b":
        r.align(4)
        val = struct.unpack(r.e + "I", r.take(4))[0]
        if val > 1:
            raise ValueError("bad boolean")
        return bool(val)
    if c in _STRUCT:
        r.align(_ALIGN[c])
        size = struct.calcsize(_STRUCT[c])
        return struct.unpack(r.e + _STRUCT[c], r.take(size))[0]
    if c in "so":
        r.align(4)
        n = struct.unpack(r.e + "I", r.take(4))[0]
        data = r.take(n)
        if r.take(1) != b"\0":
            raise ValueError("string not NUL-terminated")
        return data.decode("utf-8")
    if c == "g":
        n = r.take(1)[0]
        data = r.take(n)
        r.take(1)
        return data.decode("ascii")
    if c == "v":
        sig = _dec(r, "g")
        types = split_signature(sig)
        if len(types) != 1:
            raise ValueError("variant must hold one complete type")
        return Variant(sig, _dec(r, types[0]))
    if c == "a":
        elem = t[1:]
        r.align(4)
        n = struct.unpack(r.e + "I", r.take(4))[0]
        r.align(_ALIGN[elem[0]])
        end = r.pos + n
        if end > len(r.data):
            raise ValueError("array too long")
        if elem[0] == "{":
            kt, vt = split_signature(elem[1:-1])
            out: Any = {}
            while r.pos < end:
                r.align(8)
                key = _dec(r, kt)
                out[key] = _dec(r, vt)
        else:
            out = []
            while r.pos < end:
                out.append(_dec(r, elem))
        if r.pos != end:
            raise ValueError("array length mismatch")
        return out
    if c == "(":
        r.align(8)
        return tuple(_dec(r, part) for part in split_signature(t[1:-1]))
    raise ValueError(f"cannot unmarshal type {t!r}")


def unmarshal(signature: str, data: bytes, endian: str = "<") -> list:
    r = _Reader(data, endian)
    return [_dec(r, t) for t in split_signature(signature)]


class Message:
    def __init__(self, mtype: int, serial: int = 0, flags: int = 0, *, path: str | None = None,
                 interface: str | None = None, member: str | None = None, error_name: str | None = None,
                 reply_serial: int | None = None, destination: str | None = None, sender: str | None = None,
                 signature: str = "", body: tuple | list = ()) -> None:
        self.type, self.serial, self.flags = mtype, serial, flags
        self.path, self.interface, self.member = path, interface, member
        self.error_name, self.reply_serial = error_name, reply_serial
        self.destination, self.sender = destination, sender
        self.signature, self.body = signature, list(body)

    def __repr__(self) -> str:
        return (f"Message(type={self.type}, serial={self.serial}, path={self.path}, "
                f"iface={self.interface}, member={self.member}, sig={self.signature!r}, body={self.body})")

    def to_bytes(self) -> bytes:
        body = marshal(self.signature, self.body)
        fields: list[tuple[int, Variant]] = []
        for code, sig, val in ((F_PATH, "o", self.path), (F_INTERFACE, "s", self.interface),
                               (F_MEMBER, "s", self.member), (F_ERROR_NAME, "s", self.error_name),
                               (F_REPLY_SERIAL, "u", self.reply_serial), (F_DESTINATION, "s", self.destination),
                               (F_SENDER, "s", self.sender)):
            if val is not None:
                fields.append((code, Variant(sig, val)))
        if self.signature:
            fields.append((F_SIGNATURE, Variant("g", self.signature)))
        buf = bytearray(struct.pack("<cBBBII", b"l", self.type, self.flags, 1, len(body), self.serial))
        _enc(buf, "a(yv)", fields, "<")
        _pad(buf, 8)
        return bytes(buf) + body

    @classmethod
    def parse(cls, head: bytes, body: bytes) -> "Message":
        """head — заголовок целиком (с 16 байтами фиксированной части и выравниванием до 8)."""
        endian = "<" if head[0:1] == b"l" else ">"
        _, mtype, flags, _ver, _blen, serial = struct.unpack(endian + "cBBBII", head[:12])
        r = _Reader(head, endian)
        r.pos = 12
        fields = {code: var.value for code, var in _dec(r, "a(yv)")}
        sig = fields.get(F_SIGNATURE, "")
        msg = cls(mtype, serial, flags, path=fields.get(F_PATH), interface=fields.get(F_INTERFACE),
                  member=fields.get(F_MEMBER), error_name=fields.get(F_ERROR_NAME),
                  reply_serial=fields.get(F_REPLY_SERIAL), destination=fields.get(F_DESTINATION),
                  sender=fields.get(F_SENDER), signature=sig)
        msg.body = unmarshal(sig, body, endian) if sig else []
        return msg


# ───────────────────────── описание интерфейсов ─────────────────────────

class Interface:
    """Интерфейс объекта D-Bus.

    methods:    имя -> (сигнатура аргументов, сигнатура результата, функция)
    properties: имя -> (сигнатура, getter, setter | None); emits[имя] = 'true' | 'const' | 'false' —
                значение аннотации EmitsChangedSignal (по умолчанию 'true')
    signals:    имя -> сигнатура
    Функции могут быть обычными или async; результат метода — значение, кортеж значений или None.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.methods: dict[str, tuple[str, str, Callable]] = {}
        self.properties: dict[str, tuple[str, Callable, Callable | None]] = {}
        self.signals: dict[str, str] = {}
        self.emits: dict[str, str] = {}

    def method(self, name: str, in_sig: str, out_sig: str, func: Callable) -> None:
        self.methods[name] = (in_sig, out_sig, func)

    def prop(self, name: str, sig: str, getter: Callable, setter: Callable | None = None,
             emits: str = "true") -> None:
        self.properties[name] = (sig, getter, setter)
        self.emits[name] = emits

    def signal(self, name: str, sig: str) -> None:
        self.signals[name] = sig

    def get_all(self) -> dict[str, Variant]:
        out = {}
        for name, (sig, getter, _s) in self.properties.items():
            out[name] = Variant(sig, getter())
        return out

    def introspect(self) -> str:
        lines = [f'  <interface name={quoteattr(self.name)}>']
        for name, (in_sig, out_sig, _f) in self.methods.items():
            lines.append(f'    <method name={quoteattr(name)}>')
            lines += [f'      <arg type="{t}" direction="in"/>' for t in split_signature(in_sig)]
            lines += [f'      <arg type="{t}" direction="out"/>' for t in split_signature(out_sig)]
            lines.append('    </method>')
        for name, (sig, _g, setter) in self.properties.items():
            access = "readwrite" if setter else "read"
            emits = self.emits.get(name, "true")
            lines.append(f'    <property name={quoteattr(name)} type="{sig}" access="{access}">')
            lines.append(f'      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="{emits}"/>')
            lines.append('    </property>')
        for name, sig in self.signals.items():
            lines.append(f'    <signal name={quoteattr(name)}>')
            lines += [f'      <arg type="{t}"/>' for t in split_signature(sig)]
            lines.append('    </signal>')
        lines.append('  </interface>')
        return "\n".join(lines)


_INTROSPECT_HEADER = ('<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" '
                      '"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">\n')
_STD_XML = """  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
    <method name="GetMachineId"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
  </interface>
"""

ERR_UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
ERR_UNKNOWN_OBJECT = "org.freedesktop.DBus.Error.UnknownObject"
ERR_UNKNOWN_IFACE = "org.freedesktop.DBus.Error.UnknownInterface"
ERR_UNKNOWN_PROP = "org.freedesktop.DBus.Error.UnknownProperty"
ERR_INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"
ERR_PROP_READONLY = "org.freedesktop.DBus.Error.PropertyReadOnly"
ERR_FAILED = "org.freedesktop.DBus.Error.Failed"


# ───────────────────────── адрес шины ─────────────────────────

def _unescape(value: str) -> str:
    """Адрес D-Bus экранирует спецсимволы как %xx."""
    return unquote_to_bytes(value).decode("utf-8", "replace")


def session_bus_paths(environ: dict | None = None, uid: int | None = None) -> list[str]:
    """Пути unix-сокетов сессионной шины по порядку предпочтения.

    Абстрактные сокеты начинаются с '\\0'. Разбирается DBUS_SESSION_BUS_ADDRESS, затем XDG_RUNTIME_DIR/bus.
    """
    env = os.environ if environ is None else environ
    uid = os.getuid() if uid is None else uid
    paths: list[str] = []
    for address in (env.get("DBUS_SESSION_BUS_ADDRESS") or "").split(";"):
        address = address.strip()
        if not address.startswith("unix:"):
            continue
        params = {}
        for item in address[5:].split(","):
            key, _, value = item.partition("=")
            params[key] = _unescape(value)
        if params.get("path"):
            paths.append(params["path"])
        elif params.get("abstract"):
            paths.append("\0" + params["abstract"])
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    fallback = os.path.join(runtime, "bus")
    if fallback not in paths:
        paths.append(fallback)
    return paths


# ───────────────────────── соединение ─────────────────────────

class DBusConnection:
    """Одно соединение с шиной. Использование:

        bus = DBusConnection()
        await bus.connect()
        bus.export("/org/mpris/MediaPlayer2", iface1, iface2)
        await bus.request_name("org.mpris.MediaPlayer2.fipt")
    """

    def __init__(self, on_close: Callable[[], None] | None = None) -> None:
        self.unique_name: str | None = None
        self.on_close = on_close
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._serial = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._objects: dict[str, dict[str, Interface]] = {}
        self._read_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.signal_handlers: list[Callable[[Message], None]] = []

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing() and \
            self._read_task is not None and not self._read_task.done()

    # ---- подключение ----
    async def connect(self, paths: list[str] | None = None) -> None:
        last: Exception | None = None
        for path in paths or session_bus_paths():
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(path, limit=MAX_MESSAGE), 5.0)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
                continue
            try:
                await asyncio.wait_for(self._authenticate(), 5.0)
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._drop_socket()
                continue
            self._read_task = asyncio.create_task(self._read_loop(), name="fipt-dbus-read")
            try:
                reply = await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                        "org.freedesktop.DBus", "Hello")
            except Exception:
                await self.close()
                raise
            self.unique_name = reply[0]
            return
        raise ConnectionError(f"D-Bus session bus is not available: {last}")

    async def _authenticate(self) -> None:
        assert self._reader is not None and self._writer is not None
        uid_hex = str(os.getuid()).encode().hex()
        self._writer.write(b"\0AUTH EXTERNAL " + uid_hex.encode() + b"\r\n")
        await self._writer.drain()
        line = await self._reader.readline()
        if not line.startswith(b"OK"):
            raise ConnectionError(f"D-Bus authentication refused: {line[:80]!r}")
        self._writer.write(b"BEGIN\r\n")
        await self._writer.drain()

    def _drop_socket(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._reader = self._writer = None

    async def close(self) -> None:
        self._closing = True
        task, self._read_task = self._read_task, None
        self._drop_socket()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for t in list(self._tasks):
            t.cancel()
        self._fail_pending(ConnectionError("closed"))

    # ---- чтение ----
    async def _read_message(self) -> Message:
        assert self._reader is not None
        fixed = await self._reader.readexactly(16)
        endian = "<" if fixed[0:1] == b"l" else ">"
        if fixed[0:1] not in (b"l", b"B"):
            raise ConnectionError("bad D-Bus message")
        body_len, _serial, fields_len = struct.unpack(endian + "III", fixed[4:16])
        head_len = 16 + fields_len
        head_len += -head_len % 8
        if body_len + head_len > MAX_MESSAGE:
            raise ConnectionError("D-Bus message too large")
        rest = await self._reader.readexactly(head_len - 16 + body_len)
        head, body = fixed + rest[:head_len - 16], rest[head_len - 16:]
        return Message.parse(head, body)

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    msg = await self._read_message()
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    break
                except ValueError:
                    continue            # непонятное сообщение не должно ронять соединение
                try:
                    self._dispatch(msg)
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._fail_pending(ConnectionError("closed"))
            if not self._closing and self.on_close:
                self.on_close()

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    def _dispatch(self, msg: Message) -> None:
        if msg.type in (RETURN, ERROR):
            fut = self._pending.pop(msg.reply_serial or -1, None)
            if fut is None or fut.done():
                return
            if msg.type == RETURN:
                fut.set_result(msg.body)
            else:
                text = msg.body[0] if msg.body and isinstance(msg.body[0], str) else ""
                fut.set_exception(DBusError(msg.error_name or ERR_FAILED, text))
        elif msg.type == CALL:
            task = asyncio.create_task(self._handle_call(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif msg.type == SIGNAL:
            for handler in list(self.signal_handlers):
                handler(msg)

    # ---- отправка ----
    def _send(self, msg: Message) -> int:
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("D-Bus connection is closed")
        if not msg.serial:
            self._serial += 1
            msg.serial = self._serial
        self._writer.write(msg.to_bytes())
        return msg.serial

    async def call(self, destination: str, path: str, interface: str, member: str,
                   signature: str = "", body: tuple | list = (), timeout: float = CALL_TIMEOUT) -> list:
        msg = Message(CALL, destination=destination, path=path, interface=interface, member=member,
                      signature=signature, body=body)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        serial = self._send(msg)
        self._pending[serial] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(serial, None)
            raise DBusError("org.freedesktop.DBus.Error.Timeout", member) from None

    def emit_signal(self, path: str, interface: str, member: str, signature: str = "",
                    body: tuple | list = ()) -> None:
        try:
            self._send(Message(SIGNAL, path=path, interface=interface, member=member,
                               signature=signature, body=body))
        except ConnectionError:
            pass

    async def request_name(self, name: str) -> bool:
        """True — имя получено (мы владелец). DO_NOT_QUEUE: не вставать в очередь, если занято."""
        reply = await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "RequestName", "su", (name, 4))
        return reply[0] in (1, 4)

    async def release_name(self, name: str) -> None:
        try:
            await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "ReleaseName", "s", (name,), timeout=2.0)
        except Exception:  # noqa: BLE001
            pass

    # ---- объекты ----
    def export(self, path: str, *interfaces: Interface) -> None:
        self._objects[path] = {i.name: i for i in interfaces}

    def unexport(self, path: str) -> None:
        self._objects.pop(path, None)

    def properties_changed(self, path: str, interface: str, changed: dict[str, Variant],
                           invalidated: list[str] | None = None) -> None:
        if changed or invalidated:
            self.emit_signal(path, "org.freedesktop.DBus.Properties", "PropertiesChanged", "sa{sv}as",
                             (interface, changed, invalidated or []))

    def _introspect(self, path: str) -> str:
        ifaces = self._objects.get(path, {})
        prefix = path.rstrip("/") + "/"
        children = sorted({p[len(prefix):].split("/")[0] for p in self._objects if p != path and p.startswith(prefix)})
        xml = _INTROSPECT_HEADER + "<node>\n" + _STD_XML
        xml += "\n".join(i.introspect() for i in ifaces.values())
        xml += "".join(f'\n  <node name={quoteattr(c)}/>' for c in children)
        return xml + "\n</node>\n"

    # ---- обработка входящих вызовов ----
    async def _handle_call(self, msg: Message) -> None:
        try:
            result_sig, result = await self._invoke(msg)
            if not (msg.flags & NO_REPLY_EXPECTED):
                self._send(Message(RETURN, reply_serial=msg.serial, destination=msg.sender,
                                   signature=result_sig, body=result))
        except DBusError as exc:
            self._reply_error(msg, exc.name, exc.message)
        except ConnectionError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._reply_error(msg, ERR_FAILED, f"{type(exc).__name__}: {exc}")

    def _reply_error(self, msg: Message, name: str, text: str) -> None:
        if msg.flags & NO_REPLY_EXPECTED:
            return
        try:
            self._send(Message(ERROR, reply_serial=msg.serial, destination=msg.sender, error_name=name,
                               signature="s", body=(text,)))
        except ConnectionError:
            pass

    async def _invoke(self, msg: Message) -> tuple[str, list]:
        path, iface, member = msg.path or "", msg.interface, msg.member or ""
        # Peer есть у любого соединения, даже без объекта
        if iface == "org.freedesktop.DBus.Peer":
            if member == "Ping":
                return "", []
            if member == "GetMachineId":
                return "s", [_machine_id()]
        objects = self._objects.get(path)
        if objects is None and not any(p.startswith(path.rstrip("/") + "/") for p in self._objects):
            raise DBusError(ERR_UNKNOWN_OBJECT, f"no object at {path}")
        objects = objects or {}

        if iface in (None, "org.freedesktop.DBus.Introspectable") and member == "Introspect":
            return "s", [self._introspect(path)]
        if iface == "org.freedesktop.DBus.Properties":
            return self._properties_call(objects, msg)
        candidates = [objects[iface]] if iface in objects else (list(objects.values()) if iface is None else [])
        for candidate in candidates:
            spec = candidate.methods.get(member)
            if spec is None:
                continue
            in_sig, out_sig, func = spec
            if msg.signature != in_sig:
                raise DBusError(ERR_INVALID_ARGS, f"{member} expects '{in_sig}', got '{msg.signature}'")
            value = func(*msg.body)
            if inspect.isawaitable(value):
                value = await value
            return out_sig, _result_list(out_sig, value)
        raise DBusError(ERR_UNKNOWN_METHOD, f"{iface or ''}.{member}")

    def _properties_call(self, objects: dict[str, Interface], msg: Message) -> tuple[str, list]:
        member, body = msg.member, msg.body
        want = {"Get": "ss", "GetAll": "s", "Set": "ssv"}.get(member or "")
        if want is None:
            raise DBusError(ERR_UNKNOWN_METHOD, f"org.freedesktop.DBus.Properties.{member}")
        if msg.signature != want:
            raise DBusError(ERR_INVALID_ARGS, f"{member} expects '{want}'")
        iface = objects.get(body[0])
        if iface is None:
            raise DBusError(ERR_UNKNOWN_IFACE, body[0])
        if member == "GetAll":
            return "a{sv}", [iface.get_all()]
        spec = iface.properties.get(body[1])
        if spec is None:
            raise DBusError(ERR_UNKNOWN_PROP, body[1])
        sig, getter, setter = spec
        if member == "Get":
            return "v", [Variant(sig, getter())]
        if setter is None:
            raise DBusError(ERR_PROP_READONLY, body[1])
        if body[2].signature != sig:
            raise DBusError(ERR_INVALID_ARGS, f"{body[1]} is '{sig}'")
        setter(body[2].value)
        return "", []


def _result_list(out_sig: str, value: Any) -> list:
    n = len(split_signature(out_sig))
    if n == 0:
        return []
    if n == 1:
        return [value]
    return list(value)


_machine_id_cache: str | None = None


def _machine_id() -> str:
    global _machine_id_cache
    if _machine_id_cache is None:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, encoding="ascii") as f:
                    _machine_id_cache = f.read().strip()
                    break
            except OSError:
                continue
        else:
            _machine_id_cache = "0" * 32
    return _machine_id_cache
