"""SpriteLoom MCP server.

The MCP server is a thin adapter around :class:`SpriteService`.  It keeps one
service instance for the lifetime of the MCP process and writes every result
to a new, service-owned output directory.  The model service is started only
by a request; status checks are read-only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from PIL import Image, ImageChops
from pydantic import Field

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from spriteloom_mcp.service import SpriteService


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = Path.home() / "spriteloom-output"
MAX_DIMENSION = 512
MAX_VARIANTS = 8
MAX_SEED = 2**32 - 1
MAX_HISTORY = 100
OUTPUT_OWNER = "spriteloom-mcp"
METADATA_NAME = "metadata.json"

Dimension = Annotated[int, Field(ge=1, le=MAX_DIMENSION)]
VariantCount = Annotated[int, Field(ge=1, le=MAX_VARIANTS)]
Seed = Annotated[int | None, Field(ge=0, le=MAX_SEED)]
HistoryLimit = Annotated[int, Field(ge=1, le=MAX_HISTORY)]
Background = Literal["auto", "remove", "keep"]


class MCPServerError(RuntimeError):
    """A request could not be converted to or returned by SpriteService."""


_service: SpriteService | None = None


def _get_service() -> SpriteService:
    """Return the one resident service used by all tools in this process."""

    global _service
    if _service is None:
        # SpriteService owns starting and reusing the headless local service.
        # Constructing it must stay cheap because status is read-only.
        engine_root = os.environ.get("SPRITELOOM_ENGINE_ROOT")
        root = Path(engine_root).expanduser().resolve() if engine_root else None
        _service = SpriteService(root=root)
    return _service


def _set_service_for_tests(service: SpriteService | None) -> None:
    """Replace the shared service in tests without exposing it as an MCP tool."""

    global _service
    _service = service


def _validate_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _validate_dimension(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not 1 <= value <= MAX_DIMENSION:
        raise ValueError(f"{name} must be between 1 and {MAX_DIMENSION}")
    return value


def _validate_optional_dimension(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    return _validate_dimension(value, name)


def _validate_variants(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("variants must be an integer")
    if not 1 <= value <= MAX_VARIANTS:
        raise ValueError(f"variants must be between 1 and {MAX_VARIANTS}")
    return value


def _validate_seed(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("seed must be an integer")
    if not 0 <= value <= MAX_SEED:
        raise ValueError(f"seed must be between 0 and {MAX_SEED}")
    return value


def _validate_palette(palette: list[list[int]] | None) -> list[list[int]] | None:
    if palette is None:
        return None
    if not isinstance(palette, list) or not palette:
        raise ValueError("palette must be a non-empty list of [r, g, b] values")
    if len(palette) > 256:
        raise ValueError("palette may contain at most 256 colors")
    checked: list[list[int]] = []
    for index, color in enumerate(palette):
        if not isinstance(color, (list, tuple)) or len(color) != 3:
            raise ValueError(f"palette entry {index} must contain three values")
        values: list[int] = []
        for channel in color:
            if isinstance(channel, bool) or not isinstance(channel, int):
                raise ValueError(f"palette entry {index} must contain integers")
            if not 0 <= channel <= 255:
                raise ValueError(f"palette entry {index} values must be 0..255")
            values.append(channel)
        checked.append(values)
    return checked


def _resolve_output_dir(output_dir: str | None) -> Path:
    """Resolve an output root without allowing a run to overwrite a file."""

    raw = output_dir or os.environ.get("SPRITELOOM_OUTPUT_DIR") or os.environ.get("SPRITELOOM_OUT")
    path = Path(raw).expanduser() if raw else DEFAULT_OUTPUT_DIR
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _new_run_dir(output_dir: str | None, mode: str, request_id: str) -> Path:
    root = _resolve_output_dir(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    prefix = f"{stamp}_{mode}_{request_id[:12]}"
    # UUID entropy prevents two MCP clients from sharing a directory.  mkdir
    # with exist_ok=False also makes the ownership boundary explicit.
    for _ in range(20):
        candidate = root / f"{prefix}_{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise MCPServerError("could not create a unique SpriteLoom output directory")


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes through a same-directory temporary file and replace."""

    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def _image_to_b64(image: Image.Image) -> str:
    """Encode an input image as the protocol's base64 PNG value."""

    return base64.b64encode(_png_bytes(image)).decode("ascii")


def _image_from_raw(raw: dict[str, Any]) -> Image.Image:
    """Decode a protocol raw RGBA image without importing the model server."""

    try:
        width = int(raw["w"])
        height = int(raw["h"])
        pixels = base64.b64decode(raw["px"], validate=True)
        if width < 1 or height < 1 or len(pixels) != width * height * 4:
            raise ValueError("pixel data length does not match dimensions")
        return Image.frombytes("RGBA", (width, height), pixels)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ValueError(f"invalid raw RGBA image: {exc}") from exc


def _image_content(image: Image.Image) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(_png_bytes(image)).decode("ascii"),
        mimeType="image/png",
    )


def _json_content(value: dict[str, Any]) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(value, ensure_ascii=False, sort_keys=True),
    )


def _read_image(path_value: str, name: str = "image_path") -> Image.Image:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError(f"{name} must be a non-empty file path")
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise ValueError(f"{name} does not point to a file: {path}")
    try:
        with Image.open(path) as opened:
            if max(opened.size) > 4096:
                raise ValueError("input images must be at most 4096 pixels per side")
            return opened.convert("RGBA")
    except Exception as exc:
        raise ValueError(f"could not read {name} {path}: {exc}") from exc


def _read_mask(path_value: str, size: tuple[int, int]) -> Image.Image:
    mask = _read_image(path_value, "mask_path").convert("L")
    if mask.size != size:
        raise ValueError(
            "mask_path must have the same dimensions as image_path "
            f"({size[0]}x{size[1]})"
        )
    # The server and the MCP output both use a binary selection.  The source
    # mask is kept separate so generated pixels can be clipped exactly to it.
    return mask.point(lambda value: 255 if value >= 128 else 0, mode="L")


def _source_size(image: Image.Image) -> tuple[int, int]:
    if image.width < 1 or image.height < 1:
        raise ValueError("source image must have positive dimensions")
    return image.width, image.height


def _request_id() -> str:
    return uuid.uuid4().hex


async def _report_progress(
    context: Context | None,
    fraction: float,
    stage: str | None,
) -> None:
    if context is None:
        return
    fraction = min(max(float(fraction), 0.0), 1.0)
    report = getattr(context, "report_progress", None)
    if report is not None:
        await report(fraction * 100.0, 100.0, stage)
    if stage:
        log = getattr(context, "log", None)
        if log is not None:
            await log("info", stage)


async def _service_request(
    payload: dict[str, Any],
    context: Context | None,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()

    async def on_progress(fraction: float, stage: str | None = None) -> None:
        await _report_progress(context, fraction, stage)

    await _report_progress(context, 0.0, "Starting SpriteLoom request")
    result = await _get_service().request(payload, on_progress=on_progress)
    elapsed = time.monotonic() - started
    if not isinstance(result, dict) or result.get("type") != "result":
        raise MCPServerError("SpriteLoom returned an invalid result")
    if not isinstance(result.get("images"), list):
        raise MCPServerError("SpriteLoom result has no image list")
    await _report_progress(context, 1.0, "SpriteLoom request complete")
    return result, elapsed


def _decode_result(result: dict[str, Any]) -> tuple[list[Image.Image], list[int]]:
    images: list[Image.Image] = []
    for index, raw in enumerate(result["images"]):
        if not isinstance(raw, dict):
            raise MCPServerError(f"result image {index} is not an object")
        try:
            images.append(_image_from_raw(raw).convert("RGBA"))
        except Exception as exc:
            raise MCPServerError(f"result image {index} is invalid: {exc}") from exc

    raw_seeds = result.get("seeds", [])
    if raw_seeds is None:
        raw_seeds = []
    if not isinstance(raw_seeds, list):
        raise MCPServerError("SpriteLoom result seeds must be a list")
    seeds: list[int] = []
    for seed in raw_seeds:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise MCPServerError("SpriteLoom result contains a non-integer seed")
        seeds.append(seed)
    return images, seeds


def _dimensions(images: list[Image.Image]) -> list[dict[str, int]]:
    return [{"width": image.width, "height": image.height} for image in images]


def _base_metadata(
    *,
    request_id: str,
    mode: str,
    prompt: str,
    images: list[Image.Image],
    seeds: list[int],
    elapsed: float,
    paths: list[Path],
    run_dir: Path,
    **extra: Any,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "owner": OUTPUT_OWNER,
        "requestid": request_id,
        "request_id": request_id,
        "mode": mode,
        "prompt": prompt,
        "paths": [str(path) for path in paths],
        "seeds": seeds,
        "dimensions": _dimensions(images),
        "elapsed": round(max(0.0, elapsed), 6),
        "created_at": time.time(),
        "run_dir": str(run_dir),
    }
    metadata.update(extra)
    return metadata


def _write_images(
    run_dir: Path,
    images: list[Image.Image],
    *,
    filename_prefix: str = "variant",
) -> tuple[list[Path], list[ImageContent]]:
    paths: list[Path] = []
    previews: list[ImageContent] = []
    for index, image in enumerate(images):
        path = run_dir / f"{filename_prefix}_{index:03d}.png"
        _atomic_write(path, _png_bytes(image))
        paths.append(path)
        previews.append(_image_content(image))
    return paths, previews


def _write_metadata(run_dir: Path, metadata: dict[str, Any]) -> Path:
    path = run_dir / METADATA_NAME
    _atomic_write(
        path,
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        ),
    )
    return path


def _response(
    metadata: dict[str, Any], previews: list[ImageContent] | None = None
) -> list[TextContent | ImageContent]:
    contents: list[TextContent | ImageContent] = [_json_content(metadata)]
    if previews:
        contents.extend(previews)
    return contents


async def _run_and_save(
    *,
    payload: dict[str, Any],
    mode: str,
    prompt: str,
    output_dir: str | None,
    context: Context | None,
    source: Image.Image | None = None,
    mask: Image.Image | None = None,
    overlay_path: str | None = None,
) -> list[TextContent | ImageContent]:
    result, elapsed = await _service_request(payload, context)
    images, seeds = _decode_result(result)
    if not images:
        raise MCPServerError("SpriteLoom returned no images")

    overlays: list[Image.Image] | None = None
    if source is not None and mask is not None:
        overlays = []
        for index, patch in enumerate(images):
            if patch.size != source.size:
                raise MCPServerError(
                    "inpaint result dimensions do not match image_path "
                    f"({patch.width}x{patch.height} vs "
                    f"{source.width}x{source.height})"
                )
            clipped = patch.copy()
            clipped.putalpha(
                ImageChops.multiply(patch.getchannel("A"), mask)
            )
            overlays.append(clipped)
            images[index] = source.copy()
            images[index].alpha_composite(clipped)

    request_id = str(payload["id"])
    run_dir = _new_run_dir(output_dir, mode, request_id)
    paths, previews = _write_images(run_dir, images)
    overlay_paths: list[Path] = []
    if overlays is not None and overlay_path is not None:
        # Use only a caller-supplied name.  The path remains under the new
        # run directory, so an existing source or user file cannot be changed.
        requested_name = Path(overlay_path).name
        if not requested_name or requested_name in {".", ".."}:
            requested_name = "overlay.png"
        requested_stem = Path(requested_name).stem or "overlay"
        overlay_dir = run_dir / "overlays"
        overlay_dir.mkdir()
        for index, overlay in enumerate(overlays):
            suffix = ".png"
            name = (
                f"{requested_stem}_{index:03d}{suffix}"
                if len(overlays) > 1
                else f"{requested_stem}{suffix}"
            )
            path = overlay_dir / name
            _atomic_write(path, _png_bytes(overlay))
            overlay_paths.append(path)

    metadata = _base_metadata(
        request_id=request_id,
        mode=mode,
        prompt=prompt,
        images=images,
        seeds=seeds,
        elapsed=elapsed,
        paths=paths,
        run_dir=run_dir,
        backend=result.get("service", {}).get("backend"),
        settings=result.get("service", {}).get("settings", {}),
        engine_url=result.get("service", {}).get("url"),
        inputs={key: value for key, value in payload.items() if key != "frames"},
        **({"overlay_paths": [str(path) for path in overlay_paths]} if overlays is not None else {}),
    )
    metadata["metadata_path"] = str(run_dir / METADATA_NAME)
    _write_metadata(run_dir, metadata)
    await _report_progress(context, 1.0, "Outputs written")
    return _response(metadata, previews)


def _common_payload(
    *,
    mode: str,
    prompt: str,
    target_size: tuple[int, int],
    variants: int,
    seed: int | None,
    palette: list[list[int]] | None,
    background: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "mode": mode,
        "prompt": prompt,
        "target_size": [target_size[0], target_size[1]],
        "variants": variants,
        "seed": seed,
        "palette": palette,
        "background": background,
    }


SERVER_INSTRUCTIONS = (
    "Generate and edit pixel-art sprites with the local SpriteLoom model. "
    "Requests use one resident headless service. Results are written to new "
    "PNG files and returned as MCP image previews."
)

mcp = FastMCP("SpriteLoom", instructions=SERVER_INSTRUCTIONS, log_level="ERROR")
server = mcp


@mcp.tool(
    name="spriteloom_status",
    title="SpriteLoom status",
    description=(
        "Read the local SpriteLoom service state. This never starts the model "
        "and returns state, backend, WebSocket URL, process ID, and ownership."
    ),
    annotations=ToolAnnotations(
        title="SpriteLoom status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def spriteloom_status(
    context: Context | None = None,
) -> list[TextContent]:
    """Return service state without starting the model."""

    del context
    status = await _get_service().status()
    if not isinstance(status, dict):
        raise MCPServerError("SpriteLoom returned an invalid status")
    return [_json_content(status)]


@mcp.tool(
    name="generate_sprite",
    title="Generate sprite",
    description=(
        "Generate one or more pixel-art sprites from a prompt. Width and "
        "height are 1..512 pixels. Each result is saved in a unique output "
        "directory and returned as a PNG preview."
    ),
    annotations=ToolAnnotations(
        title="Generate sprite",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def generate_sprite(
    prompt: str,
    width: Dimension = 64,
    height: Dimension = 64,
    variants: VariantCount = 1,
    seed: Seed = None,
    palette: list[list[int]] | None = None,
    background: Background = "auto",
    output_dir: str | None = None,
    context: Context | None = None,
) -> list[TextContent | ImageContent]:
    """Generate sprites with the local text-to-image pipeline."""

    prompt = _validate_text(prompt, "prompt")
    width = _validate_dimension(width, "width")
    height = _validate_dimension(height, "height")
    variants = _validate_variants(variants)
    seed = _validate_seed(seed)
    palette = _validate_palette(palette)
    request_id = _request_id()
    payload = _common_payload(
        mode="generate",
        prompt=prompt,
        target_size=(width, height),
        variants=variants,
        seed=seed,
        palette=palette,
        background=background,
        request_id=request_id,
    )
    return await _run_and_save(
        payload=payload,
        mode="generate",
        prompt=prompt,
        output_dir=output_dir,
        context=context,
    )


@mcp.tool(
    name="edit_sprite",
    title="Edit sprite",
    description=(
        "Edit an existing sprite with an instruction. The source dimensions "
        "are used when width or height is omitted. preserve_palette keeps the "
        "source palette unless set to false. The source file is never changed."
    ),
    annotations=ToolAnnotations(
        title="Edit sprite",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def edit_sprite(
    prompt: str,
    image_path: str,
    width: Dimension | None = None,
    height: Dimension | None = None,
    variants: VariantCount = 1,
    seed: Seed = None,
    palette: list[list[int]] | None = None,
    background: Background = "auto",
    preserve_palette: bool = True,
    output_dir: str | None = None,
    context: Context | None = None,
) -> list[TextContent | ImageContent]:
    """Edit an existing image without overwriting it."""

    prompt = _validate_text(prompt, "prompt")
    source = _read_image(image_path)
    source_width, source_height = _source_size(source)
    width = _validate_optional_dimension(width, "width")
    height = _validate_optional_dimension(height, "height")
    target_size = (width or source_width, height or source_height)
    for value in target_size:
        _validate_dimension(value, "output dimension")
    variants = _validate_variants(variants)
    seed = _validate_seed(seed)
    palette = _validate_palette(palette)
    if not isinstance(preserve_palette, bool):
        raise ValueError("preserve_palette must be a boolean")
    request_id = _request_id()
    payload = _common_payload(
        mode="edit",
        prompt=prompt,
        target_size=target_size,
        variants=variants,
        seed=seed,
        palette=palette,
        background=background,
        request_id=request_id,
    )
    payload.update(
        {
            "preserve_palette": preserve_palette,
            "frames": [{"image": _image_to_b64(source)}],
        }
    )
    return await _run_and_save(
        payload=payload,
        mode="edit",
        prompt=prompt,
        output_dir=output_dir,
        context=context,
    )


@mcp.tool(
    name="inpaint_sprite",
    title="Inpaint sprite",
    description=(
        "Inpaint a selected region of an existing sprite. image_path and "
        "mask_path must have equal dimensions. The returned patch is clipped "
        "to the original binary mask, then composited with source pixels "
        "outside the mask preserved. Set overlay_path to save transparent "
        "patch PNGs as well as composite PNGs."
    ),
    annotations=ToolAnnotations(
        title="Inpaint sprite",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def inpaint_sprite(
    prompt: str,
    image_path: str,
    mask_path: str,
    variants: VariantCount = 1,
    seed: Seed = None,
    palette: list[list[int]] | None = None,
    background: Background = "auto",
    preserve_palette: bool = True,
    overlay_path: str | None = None,
    output_dir: str | None = None,
    context: Context | None = None,
) -> list[TextContent | ImageContent]:
    """Inpaint a binary selection and save a safe composite."""

    prompt = _validate_text(prompt, "prompt")
    source = _read_image(image_path)
    source_size = _source_size(source)
    for value in source_size:
        _validate_dimension(value, "source dimension")
    mask = _read_mask(mask_path, source_size)
    variants = _validate_variants(variants)
    seed = _validate_seed(seed)
    palette = _validate_palette(palette)
    if overlay_path is not None and not isinstance(overlay_path, str):
        raise ValueError("overlay_path must be a string or null")
    request_id = _request_id()
    payload = _common_payload(
        mode="inpaint",
        prompt=prompt,
        target_size=source_size,
        variants=variants,
        seed=seed,
        palette=palette,
        background=background,
        request_id=request_id,
    )
    payload["frames"] = [
        {"image": _image_to_b64(source), "mask": _image_to_b64(mask.convert("RGBA"))}
    ]
    payload["preserve_palette"] = preserve_palette
    return await _run_and_save(
        payload=payload,
        mode="inpaint",
        prompt=prompt,
        output_dir=output_dir,
        context=context,
        source=source,
        mask=mask,
        overlay_path=overlay_path,
    )


@mcp.tool(
    name="rotate_sprite",
    title="Rotate sprite view",
    description=(
        "Re-view a sprite from a requested direction with SpriteLoom's "
        "instruction mode. Provide the subject and view explicitly. This "
        "uses the model; it does not rotate pixels. image_path is never changed."
    ),
    annotations=ToolAnnotations(
        title="Rotate sprite view",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def rotate_sprite(
    subject: str,
    view: str,
    image_path: str,
    variants: VariantCount = 1,
    seed: Seed = None,
    palette: list[list[int]] | None = None,
    background: Background = "auto",
    symmetry: bool = False,
    output_dir: str | None = None,
    context: Context | None = None,
) -> list[TextContent | ImageContent]:
    """Use instruct mode to render a new view of a source sprite."""

    subject = _validate_text(subject, "subject")
    view = _validate_text(view, "view")
    source = _read_image(image_path)
    target_size = _source_size(source)
    for value in target_size:
        _validate_dimension(value, "source dimension")
    variants = _validate_variants(variants)
    seed = _validate_seed(seed)
    palette = _validate_palette(palette)
    if not isinstance(symmetry, bool):
        raise ValueError("symmetry must be a boolean")
    prompt = f"Show this same {subject} from this view: {view}. Keep its design and materials."
    request_id = _request_id()
    payload = _common_payload(
        mode="instruct",
        prompt=prompt,
        target_size=target_size,
        variants=variants,
        seed=seed,
        palette=palette,
        background=background,
        request_id=request_id,
    )
    payload.update(
        {
            "symmetry": symmetry,
            "subject": subject,
            "view": view,
            "frames": [{"image": _image_to_b64(source)}],
        }
    )
    return await _run_and_save(
        payload=payload,
        mode="rotate",
        prompt=prompt,
        output_dir=output_dir,
        context=context,
    )


@mcp.tool(
    name="list_generations",
    title="List SpriteLoom generations",
    description=(
        "List recent MCP-owned generation metadata. Only metadata files with "
        "the SpriteLoom MCP ownership marker are read; arbitrary directories "
        "and files are ignored."
    ),
    annotations=ToolAnnotations(
        title="List SpriteLoom generations",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def list_generations(
    limit: HistoryLimit = 20,
    output_dir: str | None = None,
    context: Context | None = None,
) -> list[TextContent]:
    """List metadata owned by this MCP server."""

    del context
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_HISTORY:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY}")
    root = _resolve_output_dir(output_dir)
    generations: list[dict[str, Any]] = []
    if root.is_dir():
        candidates = [
            child
            for child in root.iterdir()
            if child.is_dir() and not child.is_symlink()
        ]
        candidates.sort(key=lambda child: child.stat().st_mtime, reverse=True)
        for child in candidates:
            metadata_path = child / METADATA_NAME
            if metadata_path.is_symlink() or not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict) or metadata.get("owner") != OUTPUT_OWNER:
                continue
            # Return the recorded metadata, but only after confirming that it
            # belongs to this server's run directory.
            generations.append(metadata)
            if len(generations) >= limit:
                break
    return [_json_content({"generations": generations, "count": len(generations)})]


@mcp.tool(
    name="stop_service",
    title="Stop SpriteLoom service",
    description=(
        "Explicitly stop the shared headless SpriteLoom service. Generation "
        "tools share the service; it also stops after ten idle minutes by default."
    ),
    annotations=ToolAnnotations(
        title="Stop SpriteLoom service",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def stop_service(
    context: Context | None = None,
) -> list[TextContent]:
    """Stop the shared service only when explicitly requested."""

    del context
    result = await _get_service().stop()
    if not isinstance(result, dict):
        raise MCPServerError("SpriteLoom returned an invalid stop result")
    return [_json_content(result)]


async def main() -> None:
    """Run the MCP server on stdio."""

    await mcp.run_stdio_async()


if __name__ == "__main__":  # pragma: no cover - exercised by mcp_entry.py
    asyncio.run(main())
