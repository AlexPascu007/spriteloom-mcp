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

Spriteloom binds to `127.0.0.1:8765` on the GPU machine. With both machines
on the same [Tailscale](https://tailscale.com) tailnet, forward the port to
the tailnet on the PC (once; it persists):

```
tailscale serve --bg --tcp 8765 tcp://127.0.0.1:8765
```

Then on the Mac point the MCP server at the PC, using its MagicDNS name or
Tailscale IP (`tailscale status` lists them):

```
claude mcp add spriteloom --scope user -e SPRITELOOM_URL=ws://your-host.example:8765 -- uv run --with "mcp<2" --with websockets --with pillow /path/to/spriteloom-mcp/server.py
```

The forward is reachable by every device on the tailnet and Spriteloom has no
authentication, so only do this on a tailnet you control. Undo with
`tailscale serve --tcp=8765 off`.

Without Tailscale, an SSH tunnel works too
(`ssh -N -L 8765:127.0.0.1:8765 <user>@<pc>`, needs OpenSSH Server on the PC),
and the default `SPRITELOOM_URL` then applies.

Image inputs (`image_path`, `mask_path`) are read on the machine running the
MCP server, and outputs are written there too.

## Environment

| Variable | Default |
| --- | --- |
| `SPRITELOOM_URL` | `ws://127.0.0.1:8765` |
| `SPRITELOOM_OUT` | `~/spriteloom-output` |
