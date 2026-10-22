# spriteloom-mcp

An MCP server and Claude skill that let agents generate and edit pixel-art
sprites through a locally running [Spriteloom](https://vkarach.github.io/spriteloom)
server (FLUX.2 Klein, JSON over WebSocket).

Tools: `spriteloom_status`, `spriteloom_generate`, `spriteloom_edit`, `spriteloom_inpaint`.
Results are saved as PNGs under `~/spriteloom-output/` (override with `SPRITELOOM_OUT`).

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Spriteloom running somewhere (it needs an NVIDIA GPU; the release is a Windows .exe)

`server.py` declares its own dependencies (`mcp<2`, `websockets`, `pillow`), so
there is nothing to install.

## Register with Claude Code

```
claude mcp add spriteloom --scope user -- uv run --with "mcp<2" --with websockets --with pillow /path/to/spriteloom-mcp/server.py
```

Install the skill by copying or symlinking `skills/spriteloom` into `~/.claude/skills/`.

## Using it from another machine (e.g. a MacBook)

Spriteloom binds to `127.0.0.1:8765` on the GPU machine, so tunnel to it
instead of exposing it. From the Mac:

```
ssh -N -L 8765:127.0.0.1:8765 <user>@<windows-pc>
```

(The PC needs OpenSSH Server enabled; Tailscale is an easy way to reach it
from outside your LAN.) The MCP server runs on the Mac and connects to the
tunnel on `ws://127.0.0.1:8765`, which is the default. If the tunnel uses a
different local port, set `SPRITELOOM_URL=ws://127.0.0.1:<port>`.
Image inputs (`image_path`, `mask_path`) are read on the machine running the
MCP server, and outputs are written there too.

## Environment

| Variable | Default |
| --- | --- |
| `SPRITELOOM_URL` | `ws://127.0.0.1:8765` |
| `SPRITELOOM_OUT` | `~/spriteloom-output` |
