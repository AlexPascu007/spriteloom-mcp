# SpriteLoom MCP integration

This integration is based on SpriteLoom by Valentyn Karachevtsev, available
at [vkarach/spriteloom](https://github.com/vkarach/spriteloom) under the
Apache License 2.0. The MCP package can live in its own repository while the
local model engine lives in a separate repository.

SpriteLoom can run as a local MCP server for Codex, Claude Code, and Claude
Desktop. The MCP entry point starts the shared local WebSocket service on the
first image request. The service stays resident for later requests and uses
the same model process across clients. Aseprite and the graphical launcher
are not required.

## Install

The installer is standard-library Python. It performs a dry run by default:

```sh
python3 scripts/install-agent-integration.py --root /absolute/path/to/spriteloom-mcp --all
```

Review the paths and use `--apply` to write the selected integration:

```sh
python3 scripts/install-agent-integration.py \
  --root /absolute/path/to/spriteloom-mcp --all --apply
```

The command uses the MCP repository's `.venv/bin/python` and
`/absolute/path/to/spriteloom-mcp/mcp_entry.py`. If the model engine is in a
separate checkout, pass its path explicitly:

```sh
python3 scripts/install-agent-integration.py \
  --root /absolute/path/to/spriteloom-mcp \
  --engine-root /absolute/path/to/spriteloom-mlx \
  --all --apply
```

The installer adds `SPRITELOOM_ENGINE_ROOT` to the server environment when
`--engine-root` is used. The MCP entry point uses that value to locate the
engine. Without that option, the entry point uses its normal local engine
configuration. Refresh or restart a client after installing its skill so the
new skill can be discovered. The installer writes these entries:

| Client | MCP configuration | Skill installation |
| --- | --- | --- |
| Codex | `~/.codex/config.toml` under `[mcp_servers.spriteloom]` | `~/.codex/skills/spriteloom` |
| Claude Code | `~/.claude.json` under `.mcpServers.spriteloom` | `~/.claude/skills/spriteloom` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` under `.mcpServers.spriteloom` | MCP config only |

The installer does not claim that Claude Desktop loads `~/.claude/skills`.
Attach or configure the skill through the Desktop feature that provides skill
support, when available.

Existing configuration data is merged in place. Unrelated keys and values are
kept. Before a changed existing config is written, the installer creates a
timestamped `.bak-<timestamp>` file. Writes use a temporary file and an
atomic replace. A same-name SpriteLoom server or skill with different
contents stops the install; pass `--replace` only after reviewing that
collision. Re-running an unchanged install is a no-op. The installer tests
can use `--home` and never require a real user configuration.

The Codex entry uses these values so queued image requests have time to
finish:

```toml
[mcp_servers.spriteloom]
command = "/absolute/path/to/spriteloom-mcp/.venv/bin/python"
args = ["/absolute/path/to/spriteloom-mcp/mcp_entry.py"]
tool_timeout_sec = 600
startup_timeout_sec = 30
```

With a separate engine checkout, the Codex block also contains:

```toml
[mcp_servers.spriteloom.env]
SPRITELOOM_ENGINE_ROOT = "/absolute/path/to/spriteloom-mlx"
```

## Tool contract

The MCP server exposes the following operations. Client UIs can add a client
specific tool prefix; use the discovered names instead of hard-coding that
prefix.

| Tool | Inputs and behavior |
| --- | --- |
| `spriteloom_status` | Read-only service state. It does not auto-start the service. |
| `generate_sprite` | `prompt`; optional `width=64`, `height=64`, `variants=1`, `seed=None`, `palette=None`, `background="auto"`, and `output_dir=None`. |
| `edit_sprite` | `image_path`, `prompt`; preserve the source palette by default with `preserve_palette=True`. Optional width/height and output controls are supported by the server. |
| `inpaint_sprite` | `image_path`, `mask_path`, `prompt`; the mask has the source dimensions, with white for repaint and black for preserve. |
| `rotate_sprite` | `image_path`, `subject`, `view`; `symmetry=False` by default. This creates an explicit angle view, not an animation. |
| `list_generations` | `limit=20` and optional `output_dir`; lists saved generation records. |
| `stop_service` | Stops the shared service. Call only when the user asks for service management or GPU memory release. |

Image requests auto-start the local service and reuse its resident model.
Keep one variant unless the user asks for more, and use bounded retries only
when a concrete transient error supports one. The measured Apple M5 setup
uses eight steps and a 512-pixel working image for a 64-pixel result; a single
request takes about 17–21 seconds and reaches about 12.3 GB peak MLX
allocation. MLX and CUDA can produce different results from the same seed.
The service has a default idle timeout of ten minutes; a status call does not
extend that timeout.

Palette preservation forces output colors to the source palette. Set it to
`False` when the user asks for new colors. For inpainting, SpriteLoom writes
a new composite PNG while leaving the original file unchanged. Set
`overlay_path` to also save a transparent patch clipped to the exact mask. Results include JSON metadata and saved absolute PNG or preview
paths.

## Verification

Run the installer tests with the repository Python:

```sh
python3 scripts/test_install_agent_integration.py
```

The tests use temporary homes and roots. They cover dry-run behavior, config
merging, idempotent repeats, collision handling, timestamp backups, TOML
validation, separate engine roots, symlink skills, and paths that contain
spaces.
