"""Focused tests for the SpriteLoom MCP adapter.

These tests inject a small SpriteService double.  They do not load the model
or start a WebSocket process.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import importlib
import json
import sys
import types
from pathlib import Path

import pytest
from PIL import Image



def async_test(function):
    """Run one async test without requiring pytest-asyncio."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def image_to_raw(image: Image.Image) -> dict:
    return {
        "w": image.width,
        "h": image.height,
        "px": base64.b64encode(image.convert("RGBA").tobytes()).decode("ascii"),
    }


class FakeSpriteService:
    instances: list["FakeSpriteService"] = []

    def __init__(self, root=None):
        self.root = root
        self.calls: list[dict] = []
        self.status_calls = 0
        self.stop_calls = 0
        self.__class__.instances.append(self)

    async def status(self):
        self.status_calls += 1
        return {
            "state": "offline",
            "backend": "mlx",
            "url": "ws://127.0.0.1:8765",
            "pid": None,
            "managed": False,
        }

    async def request(self, payload, on_progress=None):
        self.calls.append(payload)
        if on_progress is not None:
            await on_progress(0.25, "Loading")
            await on_progress(0.75, "Rendering")
        width, height = payload["target_size"]
        if payload["mode"] == "inpaint":
            image = Image.new("RGBA", (width, height), (200, 10, 10, 255))
        else:
            image = Image.new("RGBA", (width, height), (20, 40, 60, 255))
        count = payload["variants"]
        return {
            "type": "result",
            "images": [image_to_raw(image) for _ in range(count)],
            "seeds": list(range(count)),
        }

    async def stop(self):
        self.stop_calls += 1
        return {"stopped": True, "reason": "test"}


@pytest.fixture()
def server_module(monkeypatch, tmp_path):
    service_module = types.ModuleType("spriteloom_mcp.service")
    service_module.SpriteService = FakeSpriteService
    monkeypatch.setitem(sys.modules, "spriteloom_mcp.service", service_module)
    sys.modules.pop("spriteloom_mcp.server", None)
    module = importlib.import_module("spriteloom_mcp.server")
    service = FakeSpriteService(root=tmp_path)
    module._set_service_for_tests(service)
    module.DEFAULT_OUTPUT_DIR = tmp_path / "default-output"
    yield module, service, tmp_path
    module._set_service_for_tests(None)


def _text(result):
    return json.loads(result[0].text)


def _write_image(path: Path, color=(10, 20, 30, 255), size=(4, 4)):
    Image.new("RGBA", size, color).save(path)


@async_test
async def test_tools_are_explicitly_registered(server_module):
    module, _, _ = server_module
    names = {tool.name for tool in module.mcp._tool_manager.list_tools()}
    assert names == {
        "spriteloom_status",
        "generate_sprite",
        "edit_sprite",
        "inpaint_sprite",
        "rotate_sprite",
        "list_generations",
        "stop_service",
    }


@async_test
async def test_status_is_read_only_and_generation_uses_expected_payload(server_module):
    module, service, tmp_path = server_module
    status = await module.spriteloom_status()
    assert _text(status)["state"] == "offline"
    assert service.status_calls == 1
    assert service.calls == []

    progress = []

    class ContextDouble:
        async def report_progress(self, value, total, message):
            progress.append((value, total, message))

        async def log(self, level, message, logger_name=None):
            progress.append((level, message))

    result = await module.generate_sprite(
        "a blue coin",
        width=8,
        height=6,
        variants=2,
        seed=42,
        palette=[[1, 2, 3]],
        background="keep",
        output_dir=str(tmp_path / "runs"),
        context=ContextDouble(),
    )
    payload = service.calls[-1]
    assert payload["mode"] == "generate"
    assert payload["target_size"] == [8, 6]
    assert payload["variants"] == 2
    assert payload["seed"] == 42
    assert payload["palette"] == [[1, 2, 3]]
    assert payload["background"] == "keep"
    metadata = _text(result)
    assert len(metadata["paths"]) == 2
    assert all(Path(path).is_file() for path in metadata["paths"])
    assert len(result) == 3  # one TextContent and two ImageContent previews
    assert progress
    assert any(entry[0] == 25.0 for entry in progress if isinstance(entry[0], float))
    assert metadata["dimensions"] == [
        {"width": 8, "height": 6},
        {"width": 8, "height": 6},
    ]


@async_test
async def test_edit_reads_source_sets_default_dimensions_and_never_overwrites(
    server_module,
):
    module, service, tmp_path = server_module
    source_path = tmp_path / "source.png"
    _write_image(source_path, size=(7, 5))
    original = source_path.read_bytes()
    result = await module.edit_sprite(
        "make the coin gold",
        str(source_path),
        variants=1,
        preserve_palette=False,
        output_dir=str(tmp_path / "runs"),
    )
    payload = service.calls[-1]
    assert payload["mode"] == "edit"
    assert payload["target_size"] == [7, 5]
    assert payload["preserve_palette"] is False
    assert payload["frames"][0]["image"]
    assert source_path.read_bytes() == original
    assert _text(result)["paths"]


@async_test
async def test_inpaint_clips_to_original_mask_and_can_save_overlay(server_module):
    module, service, tmp_path = server_module
    source_path = tmp_path / "source.png"
    mask_path = tmp_path / "mask.png"
    source = Image.new("RGBA", (4, 4), (12, 34, 56, 255))
    source.putpixel((1, 1), (70, 80, 90, 255))
    source.save(source_path)
    mask = Image.new("L", (4, 4), 0)
    mask.putpixel((1, 1), 255)
    mask.save(mask_path)
    original = source_path.read_bytes()

    result = await module.inpaint_sprite(
        "add a jewel",
        str(source_path),
        str(mask_path),
        overlay_path="patch.png",
        output_dir=str(tmp_path / "runs"),
    )
    metadata = _text(result)
    composite = Image.open(metadata["paths"][0]).convert("RGBA")
    assert composite.getpixel((0, 0)) == source.getpixel((0, 0))
    assert composite.getpixel((1, 1)) == (200, 10, 10, 255)
    overlay_path = Path(metadata["overlay_paths"][0])
    overlay = Image.open(overlay_path).convert("RGBA")
    assert overlay.getpixel((0, 0))[3] == 0
    assert overlay.getpixel((1, 1))[3] == 255
    assert source_path.read_bytes() == original
    assert service.calls[-1]["mode"] == "inpaint"
    assert service.calls[-1]["target_size"] == [4, 4]
    assert service.calls[-1]["frames"][0]["mask"]


@async_test
async def test_rotate_uses_instruct_payload_and_no_literal_pixel_rotation(server_module):
    module, service, tmp_path = server_module
    source_path = tmp_path / "source.png"
    _write_image(source_path, size=(6, 6))
    await module.rotate_sprite(
        subject="a four legged horse",
        view="rear three quarter",
        image_path=str(source_path),
        symmetry=True,
        output_dir=str(tmp_path / "runs"),
    )
    payload = service.calls[-1]
    assert payload["mode"] == "instruct"
    assert payload["subject"] == "a four legged horse"
    assert payload["view"] == "rear three quarter"
    assert payload["symmetry"] is True
    assert payload["subject"] in payload["prompt"]
    assert payload["view"] in payload["prompt"]
    assert "Show" in payload["prompt"]


@async_test
async def test_list_only_returns_owned_metadata_and_stop_is_explicit(server_module):
    module, service, tmp_path = server_module
    output_root = tmp_path / "runs"
    await module.generate_sprite("one", output_dir=str(output_root))
    await module.generate_sprite("two", output_dir=str(output_root))
    foreign = output_root / "foreign"
    foreign.mkdir()
    (foreign / "metadata.json").write_text(
        json.dumps({"owner": "some-other-tool", "prompt": "secret"}),
        encoding="utf-8",
    )
    listed = _text(await module.list_generations(output_dir=str(output_root)))
    assert listed["count"] == 2
    assert all(item["owner"] == "spriteloom-mcp" for item in listed["generations"])
    assert service.stop_calls == 0
    stopped = _text(await module.stop_service())
    assert stopped["stopped"] is True
    assert service.stop_calls == 1


@async_test
async def test_validation_and_fastmcp_tool_errors(server_module):
    module, _, tmp_path = server_module
    with pytest.raises(ValueError, match="width"):
        await module.generate_sprite("coin", width=0)
    with pytest.raises(ValueError, match="seed"):
        await module.generate_sprite("coin", seed=2**32)
    with pytest.raises(ValueError, match="same dimensions"):
        source = tmp_path / "source.png"
        mask = tmp_path / "mask.png"
        _write_image(source, size=(4, 4))
        _write_image(mask, size=(3, 3))
        await module.inpaint_sprite("coin", str(source), str(mask))

    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="width"):
        await module.mcp.call_tool(
            "generate_sprite", {"prompt": "coin", "width": 0}
        )
