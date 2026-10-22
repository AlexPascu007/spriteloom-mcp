"""Share one local SpriteLoom worker between stdio MCP clients.

This module imports no model framework. Discovery stays fast; only an image
request can start the model worker. A file lock serializes worker startup.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from filelock import FileLock, Timeout
import websockets

def _settings():
    root = (Path.home() / "Library" / "Application Support" if sys.platform == "darwin"
            else Path(os.environ.get("APPDATA", str(Path.home()))))
    try:
        value = json.loads((root / "Spriteloom" / "config.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _backend():
    chosen = os.environ.get("SPRITELOOM_BACKEND") or _settings().get("backend", "auto")
    return ("mlx" if sys.platform == "darwin" else "cuda") if chosen == "auto" else chosen


class ServiceError(RuntimeError):
    pass


class SpriteService:
    def __init__(self, root: Path | None = None, *, port: int | None = None,
                 ready_timeout: float = 120, request_timeout: float = 600):
        self.root = Path(root or os.environ.get("SPRITELOOM_ENGINE_ROOT") or
                         Path(__file__).resolve().parents[2] / "spriteloom-mlx").expanduser().resolve()
        self.port = int(os.environ.get("SPRITELOOM_PORT", _settings().get("port", 8765))) if port is None else port
        if not 1024 <= self.port <= 65535:
            raise ValueError("service port must be 1024..65535")
        self.external = bool(os.environ.get("SPRITELOOM_URL"))
        self.url = os.environ.get("SPRITELOOM_URL") or f"ws://127.0.0.1:{self.port}"
        parsed = urlsplit(self.url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("SPRITELOOM_URL must be a ws:// or wss:// URL")
        self.runtime = Path(os.environ.get("SPRITELOOM_SERVICE_DIR") or
                            Path.home() / ".cache" / "spriteloom-mcp")
        self.record = self.runtime / f"service-{self.port}.json"
        self.log = self.runtime / f"service-{self.port}.log"
        self.ready_timeout = ready_timeout
        self.request_timeout = request_timeout

    async def status(self) -> dict:
        defaults = {"pid": None, "backend": _backend(), "managed": False,
                    "url": self.url, "engine_root": str(self.root),
                    "connect_only": self.external}
        try:
            async with websockets.connect(self.url, open_timeout=2, close_timeout=1,
                                          ping_interval=None, max_size=1024 * 1024) as ws:
                await ws.send(json.dumps({"type": "ping"}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if (not isinstance(data, dict) or data.get("type") != "pong"
                    or (not self.external and (data.get("server") != "spriteloom"
                                              or data.get("protocol") != 1))):
                raise ServiceError("the local port is occupied by an incompatible service; restart SpriteLoom")
            return {**defaults, **data, "state": data["model"], "url": self.url,
                    "engine_root": str(self.root), "connect_only": self.external}
        except OSError:
            return {**defaults, "state": "offline"}
        except (ServiceError, ValueError, KeyError, TimeoutError,
                websockets.exceptions.WebSocketException) as exc:
            return {**defaults, "state": "unavailable", "message": str(exc)}

    @asynccontextmanager
    async def _startup_lock(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.record) + ".lock", mode=0o600)
        deadline = time.monotonic() + self.ready_timeout
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() >= deadline:
                    raise ServiceError("timed out waiting for model service startup") from None
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            lock.release()

    def _read_record(self) -> dict:
        try:
            value = json.loads(self.record.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_record(self, data):
        fd, name = tempfile.mkstemp(prefix="service-", suffix=".json", dir=self.runtime)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
            os.replace(name, self.record)
        finally:
            Path(name).unlink(missing_ok=True)

    def _spawn(self):
        python = self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not (self.root / "server" / "main.py").is_file() or not python.is_file():
            raise ServiceError("set SPRITELOOM_ENGINE_ROOT to a spriteloom-mlx checkout with its .venv installed")
        token = uuid.uuid4().hex
        environment = os.environ.copy()
        environment.pop("SPRITELOOM_PARENT_PID", None)
        environment["SPRITELOOM_SERVICE_TOKEN"] = token
        environment["SPRITELOOM_PORT"] = str(self.port)
        environment.setdefault("SPRITELOOM_IDLE_SECONDS", "600")
        options = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        with self.log.open("ab", buffering=0) as log:
            proc = subprocess.Popen(
                [str(python), "-u", "-m", "server.main"], cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment,
                **options)
        self._write_record({"pid": proc.pid, "token": token, "root": str(self.root)})
        return proc

    def _load_error(self) -> str:
        try:
            with self.log.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 4000))
                return stream.read().decode("utf-8", errors="replace")
        except OSError:
            return "no worker log is available"

    async def ensure_ready(self, on_progress=None):
        status = await self.status()
        if status["state"] == "ready":
            return status
        process = None
        async with self._startup_lock():
            status = await self.status()
            if status["state"] == "unavailable":
                raise ServiceError(status["message"])
            if status["state"] == "offline":
                if self.external:
                    raise ServiceError("the configured SPRITELOOM_URL is offline; connect-only mode does not start a worker")
                process = self._spawn()
            # Keep the lock until the child is listening. A second client
            # must not observe an offline port and start a second worker.
            deadline = time.monotonic() + self.ready_timeout
            while status["state"] == "offline":
                if process and process.poll() is not None:
                    raise ServiceError(f"model worker exited during startup:\n{self._load_error()}")
                if time.monotonic() >= deadline:
                    raise ServiceError(f"model service did not start; see {self.log}")
                await asyncio.sleep(0.2)
                status = await self.status()
        deadline = time.monotonic() + self.ready_timeout
        while status["state"] != "ready":
            if status["state"] in ("offline", "unavailable"):
                raise ServiceError(f"model service failed to load:\n{self._load_error()}")
            if time.monotonic() >= deadline:
                raise ServiceError(f"model is still loading; see {self.log}")
            if on_progress:
                await on_progress(0, status.get("stage", "Loading local model"))
            await asyncio.sleep(0.5)
            status = await self.status()
        return status

    async def request(self, payload: dict, on_progress=None) -> dict:
        status = await self.ensure_ready(on_progress)
        if not payload.get("preserve_palette", True) and not status.get("capabilities", {}).get("palette_control"):
            raise ServiceError("this engine cannot disable palette preservation; update spriteloom-mlx")
        deadline = time.monotonic() + self.request_timeout
        async with websockets.connect(self.url, open_timeout=5, close_timeout=1,
                                      ping_interval=None, max_size=64 * 2**20) as ws:
            await ws.send(json.dumps(payload))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ServiceError("image request timed out")
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                except TimeoutError:
                    raise ServiceError("image request timed out") from None
                if message.get("type") == "error":
                    raise ServiceError(message.get("message", "image request failed"))
                if message.get("type") == "result":
                    message["service"] = status
                    return message
                if message.get("type") == "progress" and on_progress:
                    await on_progress(float(message.get("value", 0)), message.get("stage"))

    async def stop(self) -> dict:
        if self.external:
            return {"stopped": False, "reason": "connect-only services must be stopped by their owner"}
        async with self._startup_lock():
            status = await self.status()
            if status["state"] == "offline":
                return {"stopped": False, "reason": "service is already offline"}
            record = self._read_record()
            if (not status.get("managed") or status.get("pid") != record.get("pid")
                    or not record.get("token")):
                return {"stopped": False, "reason": "this service is not managed by SpriteLoom MCP"}
            async with websockets.connect(self.url, open_timeout=3, close_timeout=1,
                                          ping_interval=None) as ws:
                await ws.send(json.dumps({"type": "shutdown", "token": record["token"]}))
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if message.get("type") == "error":
                return {"stopped": False, "reason": message["message"]}
            if message.get("stopped"):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (await self.status())["state"] == "offline":
                        break
                    await asyncio.sleep(0.1)
                self.record.unlink(missing_ok=True)
                return {"stopped": True, "reason": "local model service stopped"}
            raise ServiceError("unexpected service shutdown response")
