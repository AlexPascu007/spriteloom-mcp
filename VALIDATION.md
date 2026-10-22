# Validation

Test date: 2026-10-23. Platform: macOS, Apple M5, 32 GB unified memory.

- Engine: 232 tests passed; two skipped.
- MCP and installer: 17 tests passed.
- Skill format validation passed.
- Real stdio MCP initialization returned seven tools, with no internal
  context parameter in the public schemas.
- Real MCP image requests passed for generate, edit, inpaint, and rotate.
  Each returned a PNG preview, saved paths, seed, and metadata.
- An edit with palette preservation disabled changed the green robot to blue.
- The inpaint composite matched every source pixel outside the binary mask.
- The original source file hash was unchanged after all operations.
- Managed shutdown stopped the engine and released its listening port.
- All three installed client configurations parsed and used the same tested
  executable and entry point. Client applications need a restart to load them.

The generated view changed the image, but did not give a clear side profile.
This is a model-quality limit; the view tool sends an explicit instruction
and does not guarantee a geometrically correct rotation.

Run the live check after engine setup:

```sh
.venv/bin/python scripts/validate-live.py
```

This check loads the model, writes four images under `output/validation`,
and stops the managed engine. It does not change the source image.
Tests of simultaneous clients use a small fake worker to verify that startup
creates one shared process. The full graphical client workflows were not
retested as part of this stdio check.

## Clean installation check

A separate check used fresh clones from both public GitHub repositories.
Both sets of dependencies installed in new Python 3.12 environments.
The MCP source distribution and wheel built successfully. The wheel was
installed in a third environment and run from outside the source checkout.
It exposed seven tools, started the clean MLX engine, generated one PNG with
a preview, and stopped the managed engine. Only the downloaded model files
were reused; the engine Python environment was new.

The fresh MCP checkout passed all 17 tests. Claude Code reported the installed
server as connected. Codex showed the configured stdio command and timeouts.
Claude Desktop configuration was parsed; its graphical workflow still needs
a client restart and a user check.
