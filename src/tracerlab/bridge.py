"""HTTP long-poll bridge to the PT script module.

If another tracerlab already owns the port, jobs are relayed through it (POST /exec).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

log = logging.getLogger("tracerlab.bridge")

HOST = "127.0.0.1"
PORT = int(os.environ.get("TRACERLAB_PORT") or os.environ.get("PTMCP_PORT") or "39150")
POLL_HOLD_S = 20.0           # how long a /poll request is held open when idle
STALE_AFTER_S = POLL_HOLD_S + 10.0
PICKUP_S = 8.0                # a queued job must be picked up by a poll within this
MAX_BODY = 64 * 1024 * 1024


class PTError(RuntimeError):
    """Raised for errors reported by Packet Tracer or the bridge."""


class NotConnected(PTError):
    pass


@dataclass
class _Job:
    id: int
    code: str
    future: asyncio.Future
    picked: asyncio.Event = field(default_factory=asyncio.Event)
    created: float = field(default_factory=time.monotonic)


def _bridge_js() -> str:
    return resources.files("tracerlab").joinpath("js/bridge.js").read_text()


def _bridge_build() -> str:
    import hashlib
    return hashlib.sha1(_bridge_js().encode()).hexdigest()[:10]


def _runtime_js() -> str:
    return resources.files("tracerlab").joinpath("js/runtime.js").read_text()


def _runtime_version(js: str) -> str:
    # include the hash so edits to runtime.js reload without a version bump
    import hashlib
    from . import __version__
    return f"{__version__}+{hashlib.sha1(js.encode()).hexdigest()[:8]}"


class Bridge:
    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        self.host, self.port = host, port
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._inflight: dict[int, _Job] = {}
        self._ids = itertools.count(1)
        self._chunks: dict[int, dict[int, str]] = {}
        self._server: asyncio.base_events.Server | None = None
        self._last_seen = 0.0
        self._pt_info: dict[str, Any] = {}
        self._runtime_loaded_for: str | None = None   # session token of the PT side
        self._runtime_lock = asyncio.Lock()
        self.mode = "stopped"                         # "owner" | "relay" | "stopped"

    async def start(self) -> None:
        # created here so they bind to the running loop
        self._queue = asyncio.Queue()
        self._inflight = {}
        self._chunks: dict[int, dict[int, str]] = {}
        self._runtime_lock = asyncio.Lock()
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
            self.mode = "owner"
            log.info("bridge listening on http://%s:%d", self.host, self.port)
        except OSError:
            if await asyncio.to_thread(self._probe_owner):
                self.mode = "relay"
                log.info("port %d owned by another ptmcp; relaying", self.port)
            else:
                raise PTError(f"port {self.port} is in use by something that is not ptmcp "
                              "(set PTMCP_PORT to another port and update the PT module)")

    async def stop(self) -> None:
        self._last_seen = 0.0
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for job in list(self._inflight.values()):
            if not job.future.done():
                job.future.set_exception(PTError("bridge stopped"))

    def _probe_owner(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/ping", timeout=2) as r:
                return json.loads(r.read()).get("service") == "ptmcp"
        except Exception:
            return False

    @property
    def connected(self) -> bool:
        return time.monotonic() - self._last_seen < STALE_AFTER_S

    def status(self) -> dict[str, Any]:
        if self.mode == "relay":
            try:
                with urllib.request.urlopen(f"http://{self.host}:{self.port}/ping", timeout=2) as r:
                    d = json.loads(r.read())
                    d["mode"] = "relay"
                    return d
            except Exception as e:  # pragma: no cover - network edge
                return {"mode": "relay", "connected": False, "error": str(e)}
        return {
            "service": "ptmcp",
            "mode": self.mode,
            "connected": self.connected,
            "seconds_since_poll": round(time.monotonic() - self._last_seen, 1) if self._last_seen else None,
            "pt": self._pt_info,
            "queued": self._queue.qsize(),
            "inflight": len(self._inflight),
        }

    async def run(self, code: str, timeout: float = 60.0, *, raw: bool = False) -> Any:
        """Run a JS function body in PT (PTM in scope) and return its JSON value."""
        if not raw:
            await self._ensure_runtime(timeout)
        return await self._submit(code, timeout)

    async def wait_connected(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.mode == "relay" or self.connected:
                return True
            await asyncio.sleep(0.25)
        return False

    async def _ensure_runtime(self, timeout: float) -> None:
        async with self._runtime_lock:
            probe = "return (typeof PTM !== 'undefined' && PTM.VERSION) ? PTM.VERSION : null;"
            v = await self._submit(probe, timeout)
            js = _runtime_js()
            want = _runtime_version(js)
            if v != want:
                await self._submit(js + f"\nPTM.VERSION = {json.dumps(want)};\nreturn PTM.VERSION;", timeout)

    async def _submit(self, code: str, timeout: float) -> Any:
        if self.mode == "relay":
            return await asyncio.to_thread(self._relay, code, timeout)
        if not self.connected:
            raise NotConnected(
                "Packet Tracer is not connected. Open Packet Tracer 9 with the PT-MCP script "
                "module installed (Extensions > PT-MCP Bridge shows a green 'connected' pill). "
                "See pt_setup_instructions.")
        loop = asyncio.get_running_loop()
        job = _Job(next(self._ids), code, loop.create_future())
        self._inflight[job.id] = job
        await self._queue.put(job)
        try:
            await asyncio.wait_for(job.picked.wait(), PICKUP_S)
        except asyncio.TimeoutError:
            self._inflight.pop(job.id, None)
            job.future.cancel()
            self._last_seen = 0.0
            raise NotConnected("Packet Tracer stopped polling (PT closed, bridge window closed, or a modal "
                               "dialog is blocking PT). Check the 'PT-MCP Bridge' window.") from None
        try:
            env = await asyncio.wait_for(job.future, timeout)
        except asyncio.TimeoutError:
            raise PTError(f"Packet Tracer did not answer within {timeout:.0f}s "
                          "(a modal dialog open in PT blocks scripting)") from None
        finally:
            self._inflight.pop(job.id, None)
        return _unwrap(env)

    def _relay(self, code: str, timeout: float) -> Any:
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/exec",
            data=json.dumps({"code": code, "timeout": timeout}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout + 5) as r:
                env = json.loads(r.read())
        except urllib.error.HTTPError as e:
            env = json.loads(e.read() or b"{}")
        if env.get("bridge_error"):
            raise (NotConnected if env.get("not_connected") else PTError)(env["bridge_error"])
        return _unwrap(env)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in
                       (l.split(":", 1) for l in lines[1:] if ":" in l)}
            n = int(headers.get("content-length", "0") or 0)
            if n > MAX_BODY:
                return await self._send(writer, 413, {"error": "too large"})
            if "chunked" in headers.get("transfer-encoding", "").lower():
                body = await _read_chunked(reader)
            else:
                body = await reader.readexactly(n) if n else b""
            path = target.split("?", 1)[0]
            if method == "OPTIONS":
                return await self._send(writer, 204, None)
            if path == "/ping":
                return await self._send(writer, 200, self.status())
            if path == "/hello":
                from urllib.parse import parse_qs
                q = parse_qs(target.split("?", 1)[1]) if "?" in target else {}
                self._pt_info["webview"] = {k: v[0] for k, v in q.items()}
                log.info("PT web view hello: %s", self._pt_info["webview"])
                return await self._send(writer, 200, {"ok": True})
            if path == "/poll":
                return await self._poll(writer, target)
            if path == "/result" and method == "POST":
                self._last_seen = time.monotonic()
                self._on_result(json.loads(body))
                return await self._send(writer, 200, {"ok": True})
            if path == "/exec" and method == "POST":
                return await self._exec_for_relay(writer, json.loads(body))
            if path == "/r":           # chunked GET result from the PT web view
                self._last_seen = time.monotonic()
                self._on_chunk(target)
                return await self._send_raw(writer, 200, "application/javascript", b"0;")
            if path == "/bridge.js":
                js = _bridge_js().replace("__PORT__", str(self.port)).replace("__BUILD__", _bridge_build())
                return await self._send_raw(writer, 200, "application/javascript", js.encode())
            if path == "/log":
                log.info("PT web view: %s", target)
                return await self._send(writer, 200, {"ok": True})
            if path in ("/", "/bridge"):
                return await self._send_raw(writer, 200, "text/html; charset=utf-8",
                                            _bridge_page(self.port).encode())
            await self._send(writer, 404, {"error": "not found"})
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:  # pragma: no cover - defensive
            log.exception("http handler failed")
            try:
                await self._send(writer, 500, {"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _poll(self, writer: asyncio.StreamWriter, target: str) -> None:
        self._last_seen = time.monotonic()
        q: dict[str, str] = {}
        if "?" in target:
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(target.split("?", 1)[1]).items()}
            if q.get("pt"):
                self._pt_info.update({"version": q.get("pt"), "session": q.get("s")})
        try:
            job = await asyncio.wait_for(self._queue.get(), POLL_HOLD_S)
        except asyncio.TimeoutError:
            self._last_seen = time.monotonic()
            return await self._reply_poll(writer, q, {"id": 0})
        if job.future.done():          # caller gave up while queued
            return await self._reply_poll(writer, q, {"id": 0})
        job.picked.set()
        self._last_seen = time.monotonic()
        try:
            await self._reply_poll(writer, q, {"id": job.id, "code": job.code})
        except ConnectionError:
            await self._queue.put(job)  # web view vanished mid-poll; retry on next poll

    async def _reply_poll(self, writer, q: dict, obj: dict) -> None:
        cb = q.get("cb", "")
        if cb and cb.replace("_", "").isalnum():   # JSONP for the PT web view
            body = f"{cb}({json.dumps({**obj, 'build': _bridge_build()})});".encode()
            return await self._send_raw(writer, 200, "application/javascript", body)
        await self._send(writer, 200, obj)

    def _on_chunk(self, target: str) -> None:
        from urllib.parse import parse_qs
        q = {k: v[0] for k, v in parse_qs(target.split("?", 1)[1], keep_blank_values=True).items()}
        jid, i, n = int(q["id"]), int(q["i"]), int(q["n"])
        parts = self._chunks.setdefault(jid, {})
        parts[i] = q.get("d", "")
        if len(parts) == n:
            del self._chunks[jid]
            self._on_result(json.loads("".join(parts[k] for k in range(n))))

    def _on_result(self, msg: dict) -> None:
        job = self._inflight.get(int(msg.get("id", 0)))
        if job and not job.future.done():
            job.future.set_result(msg)

    async def _exec_for_relay(self, writer, req: dict) -> None:
        try:
            value = await self._submit(req["code"], float(req.get("timeout", 60)))
            await self._send(writer, 200, {"ok": True, "value": value})
        except NotConnected as e:
            await self._send(writer, 200, {"bridge_error": str(e), "not_connected": True})
        except PTError as e:
            await self._send(writer, 200, {"bridge_error": str(e)})

    async def _send(self, writer, status: int, obj: Any) -> None:
        body = b"" if obj is None else json.dumps(obj).encode()
        await self._send_raw(writer, status, "application/json", body)

    async def _send_raw(self, writer, status: int, ctype: str, body: bytes) -> None:
        reason = {200: "OK", 204: "No Content", 404: "Not Found", 413: "Too Large",
                  500: "Error"}.get(status, "OK")
        hdr = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
               f"Content-Length: {len(body)}\r\nAccess-Control-Allow-Origin: *\r\n"
               "Access-Control-Allow-Headers: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
               "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
        writer.write(hdr.encode() + body)
        await writer.drain()


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    while True:
        size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
        if size == 0:
            await reader.readline()
            return bytes(body)
        if len(body) + size > MAX_BODY:
            raise ConnectionError("body too large")
        body += await reader.readexactly(size)
        await reader.readline()


def _unwrap(env: dict) -> Any:
    """PT side replies {ok, value} or {ok:false, error, line}."""
    if isinstance(env, str):
        env = json.loads(env)
    if env.get("ok"):
        return env.get("value")
    msg = env.get("error") or "unknown Packet Tracer error"
    if env.get("line"):
        msg += f" (line {env['line']})"
    raise PTError(msg)


def _bridge_page(port: int) -> str:
    return resources.files("tracerlab").joinpath("js/bridge.html").read_text().replace("__PORT__", str(port))
