import asyncio
from contextlib import asynccontextmanager
import json

import pytest
import websockets

from spriteloom_mcp.service import ServiceError, SpriteService


@asynccontextmanager
async def endpoint(handler):
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        yield server.sockets[0].getsockname()[1]


def test_unrelated_listener_is_not_reused_or_replaced(monkeypatch, tmp_path):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "pong", "model": "ready"}))
    async def run():
        async with endpoint(handler) as port:
            service = SpriteService(tmp_path, port=port)
            assert (await service.status())["state"] == "unavailable"
            with pytest.raises(ServiceError, match="incompatible"):
                await service.ensure_ready()
    monkeypatch.delenv("SPRITELOOM_URL", raising=False)
    monkeypatch.setenv("SPRITELOOM_SERVICE_DIR", str(tmp_path / "runtime"))
    asyncio.run(run())


def test_concurrent_clients_start_one_worker(monkeypatch, tmp_path):
    state = {"state": "offline", "starts": 0}
    async def status(self):
        return {"state": state["state"], "url": self.url}
    def spawn(self):
        state["starts"] += 1
        state["state"] = "loading"
        asyncio.get_running_loop().call_later(0.05, lambda: state.update(state="ready"))
        return type("Process", (), {"poll": lambda self: None})()
    async def run():
        clients = [SpriteService(tmp_path, port=18765) for _ in range(2)]
        await asyncio.gather(*(client.ensure_ready() for client in clients))
        assert state["starts"] == 1
    monkeypatch.setenv("SPRITELOOM_SERVICE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("SPRITELOOM_URL", raising=False)
    monkeypatch.setattr(SpriteService, "status", status)
    monkeypatch.setattr(SpriteService, "_spawn", spawn)
    asyncio.run(run())


def test_cancelled_request_closes_worker_socket(monkeypatch, tmp_path):
    closed = asyncio.Event()
    async def handler(ws):
        request = json.loads(await ws.recv())
        if request["type"] == "ping":
            await ws.send(json.dumps({"type": "pong", "model": "ready", "protocol": 1,
                                      "server": "spriteloom"}))
        else:
            await ws.send(json.dumps({"type": "progress", "value": 0.25}))
            await ws.wait_closed()
            closed.set()
    class Cancelled(Exception):
        pass
    async def progress(*_args):
        raise Cancelled()
    async def run():
        async with endpoint(handler) as port:
            service = SpriteService(tmp_path, port=port)
            with pytest.raises(Cancelled):
                await service.request({"type": "generate"}, progress)
            await asyncio.wait_for(closed.wait(), 2)
    monkeypatch.delenv("SPRITELOOM_URL", raising=False)
    asyncio.run(run())


def test_stop_does_not_signal_an_unmanaged_service(monkeypatch, tmp_path):
    async def status(self):
        return {"state": "ready", "managed": False, "pid": 123}
    monkeypatch.setenv("SPRITELOOM_SERVICE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("SPRITELOOM_URL", raising=False)
    monkeypatch.setattr(SpriteService, "status", status)
    result = asyncio.run(SpriteService(tmp_path).stop())
    assert not result["stopped"]
    assert "not managed" in result["reason"]
