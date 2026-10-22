# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "websockets>=13", "pillow"]
# ///
"""MCP server exposing a locally running Spriteloom server (FLUX.2 Klein) to agents.

Spriteloom speaks JSON over ws://127.0.0.1:8765 (see its server/protocol.py).
Sprites are written to disk as PNGs and the file paths are returned.

Env: SPRITELOOM_URL (default ws://127.0.0.1:8765), SPRITELOOM_OUT (output dir).
"""
import base64
import io
import json
import os
import pathlib
import time
import uuid

import websockets
from mcp.server.fastmcp import FastMCP
from PIL import Image

URL = os.environ.get("SPRITELOOM_URL", "ws://127.0.0.1:8765")
OUT = pathlib.Path(os.environ.get(
    "SPRITELOOM_OUT", pathlib.Path.home() / "spriteloom-output"))

mcp = FastMCP("spriteloom")


def _b64(path: str) -> str:
    buf = io.BytesIO()
    Image.open(path).convert("RGBA").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def _status() -> dict:
    async with websockets.connect(URL, open_timeout=5) as ws:
        await ws.send(json.dumps({"type": "ping"}))
        return json.loads(await ws.recv())


async def _run(mode, prompt, width, height, variants, seed, background,
               symmetry, palette, image=None, mask=None) -> dict:
    req = {"id": uuid.uuid4().hex[:8], "mode": mode, "prompt": prompt,
           "target_size": [width, height], "variants": variants,
           "background": background, "symmetry": symmetry}
    if seed is not None:
        req["seed"] = seed
    if palette:
        req["palette"] = palette
    if image:
        req["frames"] = [{"image": _b64(image),
                          **({"mask": _b64(mask)} if mask else {})}]
    try:
        ws = await websockets.connect(URL, open_timeout=5,
                                      max_size=64 * 2**20, ping_interval=None)
    except OSError as e:
        raise RuntimeError(f"Spriteloom server not reachable at {URL}: {e}. "
                           "Start Spriteloom.exe and wait for 'ready'.") from e
    async with ws:
        await ws.send(json.dumps(req))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "error":
                raise RuntimeError(msg.get("message", "unknown error"))
            if msg.get("type") == "result":
                folder = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}_{req['id']}"
                folder.mkdir(parents=True, exist_ok=True)
                paths = []
                for n, im in enumerate(msg["images"]):
                    img = Image.frombytes("RGBA", (im["w"], im["h"]),
                                          base64.b64decode(im["px"]))
                    p = folder / f"variant_{n}.png"
                    img.save(p)
                    paths.append(str(p))
                return {"images": paths, "seeds": msg.get("seeds", []),
                        "size": [width, height]}
    raise RuntimeError("connection closed before a result arrived")


@mcp.tool()
async def spriteloom_status() -> dict:
    """Check whether the Spriteloom server is up and the Klein model is loaded.
    Returns {"type":"pong","model":"ready"|"loading",...}."""
    try:
        return await _status()
    except OSError as e:
        return {"model": "unreachable", "error": str(e)}


@mcp.tool()
async def spriteloom_generate(
        prompt: str, width: int = 64, height: int = 64, variants: int = 4,
        seed: int | None = None, background: str = "auto",
        symmetry: bool = False,
        palette: list[list[int]] | None = None) -> dict:
    """Generate pixel-art sprites from text. Output is downscaled to
    width x height with a snapped palette. background: auto|remove|keep.
    palette: optional [[r,g,b],...] (max 256) to pin colors. variants 1-8.
    Returns PNG file paths (one per variant) and the seeds used."""
    return await _run("generate", prompt, width, height, variants, seed,
                      background, symmetry, palette)


@mcp.tool()
async def spriteloom_edit(
        prompt: str, image_path: str, width: int = 64, height: int = 64,
        variants: int = 4, seed: int | None = None, background: str = "auto",
        symmetry: bool = False,
        palette: list[list[int]] | None = None) -> dict:
    """Edit an existing sprite by instruction (e.g. 'give it a red cape',
    'make it face left'). image_path is a local PNG. Useful for animation
    frames and variations. Returns PNG file paths and seeds."""
    return await _run("instruct", prompt, width, height, variants, seed,
                      background, symmetry, palette, image=image_path)


@mcp.tool()
async def spriteloom_inpaint(
        prompt: str, image_path: str, mask_path: str, variants: int = 4,
        seed: int | None = None,
        palette: list[list[int]] | None = None) -> dict:
    """Regenerate only the masked region of a sprite. mask_path is a PNG the
    same size as image_path, white = region to repaint, black = keep. Output
    is input-sized and transparent outside the mask (a layer with just the new content)."""
    w, h = Image.open(image_path).size
    return await _run("inpaint", prompt, w, h, variants, seed, "keep", False,
                      palette, image=image_path, mask=mask_path)


if __name__ == "__main__":
    mcp.run()
