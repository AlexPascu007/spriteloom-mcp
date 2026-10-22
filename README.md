# spriteloom-mcp

Give Claude, Codex, and other MCP clients local tools to create and edit
pixel-art sprites. This repository contains a small stdio MCP server and
an agent skill. The model runs in the separate
[spriteloom-mlx](https://github.com/AlexPascu007/spriteloom-mlx) engine.
Aseprite is optional.

**Based on [SpriteLoom](https://github.com/vkarach/spriteloom), created by
Valentyn Karachevtsev (vkarach).** The original project supplies the sprite
workflow, prompts, image processing, protocol, and Aseprite extension.
This independent integration adds agent tools and client setup. See [NOTICE](NOTICE)
and the [Apache 2.0 license](LICENSE).

## Install

First set up the engine on Apple Silicon:

```sh
git clone https://github.com/AlexPascu007/spriteloom-mlx.git
cd spriteloom-mlx
./setup-mlx.sh
cd ..
git clone https://github.com/AlexPascu007/spriteloom-mcp.git
cd spriteloom-mcp
uv venv --python 3.12
uv pip install -e .
```

Keep the two repositories in the same parent directory, or set
`SPRITELOOM_ENGINE_ROOT` to the absolute engine path. The MCP environment
contains no model runtime. The engine uses its own Python environment.

Preview the client changes, then apply them:

```sh
.venv/bin/python scripts/install-agent-integration.py --all --engine-root ../spriteloom-mlx
.venv/bin/python scripts/install-agent-integration.py --all --engine-root ../spriteloom-mlx --apply
```

The installer supports Codex, Claude Code, and Claude Desktop on macOS.
It preserves other configuration entries and makes backups before changes.
Use `--replace` to update an existing SpriteLoom entry. Restart the clients
after installation. The skill is installed for Codex and Claude Code.
Claude Desktop receives the MCP tools; see [MCP.md](MCP.md) for skill use
and manual configuration.

For another MCP client, use the absolute path to `.venv/bin/python` as the
command, with the absolute path to `mcp_entry.py` as its only argument.
Set `SPRITELOOM_ENGINE_ROOT` in the server environment.

## Tools

| Tool | Purpose |
| --- | --- |
| `spriteloom_status` | Check service state without loading the model |
| `generate_sprite` | Create a sprite from text |
| `edit_sprite` | Edit an existing sprite by instruction |
| `inpaint_sprite` | Redraw a mask and save a complete sprite |
| `rotate_sprite` | Generate a requested view of a subject |
| `list_generations` | List saved runs and their metadata |
| `stop_service` | Release the managed engine when it is idle |

Image tools return PNG previews, absolute output paths, seeds, and metadata.
Each run has its own folder under `~/spriteloom-output`. Source files remain
unchanged. Default output is 64 by 64 pixels with one variant. Output sides
can be 1–512 pixels; variants can be 1–8. Larger work takes more time.

Editing preserves the source palette by default. Set `preserve_palette=false`
for new colors. Inpaint masks use white for the area to redraw and black for
the area to preserve. The complete output keeps every pixel outside the mask.
View changes depend on model quality; they do not produce an animation.

## Service behavior

The first image request starts the headless engine. Multiple MCP clients
share one engine and one model allocation. Requests run in sequence.
The engine stops after ten idle minutes by default. Status checks do not
reset this timer. `stop_service` can stop only a managed, idle engine.
It does not stop an independently launched engine.

The default connection is local, at `127.0.0.1:8765`. Standard output carries
only MCP messages. Engine logs go to `~/.cache/spriteloom-mcp`.
No cloud image API or API key is needed.

Environment settings:

- `SPRITELOOM_ENGINE_ROOT`: engine checkout with its `.venv` and model cache.
- `SPRITELOOM_OUTPUT_DIR`: output directory; legacy `SPRITELOOM_OUT` also works.
- `SPRITELOOM_PORT`: local engine port.
- `SPRITELOOM_IDLE_SECONDS`: idle shutdown delay; `0` disables it.
- `SPRITELOOM_URL`: explicit WebSocket connection to an existing engine.
  This mode does not start or stop the engine. Use a trusted connection.

## Development

```sh
uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

This server uses the official Python MCP SDK, pinned to `1.30.0`.
Its design follows patterns from the official
[filesystem server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)
and [GitHub MCP server](https://github.com/github/github-mcp-server): a separate
stdio process, clear tool schemas, tool annotations, and explicit client setup.

See [VALIDATION.md](VALIDATION.md) for local test results and model limits.

Version 0.2 replaces the earlier `spriteloom_generate`, `spriteloom_edit`, and
`spriteloom_inpaint` names with the names above. Inpaint now returns a complete
edited sprite; set `overlay_path` to save a separate transparent patch. Update saved prompts after upgrading.
Model weights and Aseprite binaries are not distributed in this repository.
