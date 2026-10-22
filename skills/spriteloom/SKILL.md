---
name: spriteloom
description: Generate and edit local pixel-art sprites through the SpriteLoom MCP server, including masked inpainting and explicit angle views. Use for SpriteLoom sprite work; do not route generic image generation or animation requests here.
---

# SpriteLoom

Use the discovered SpriteLoom MCP tool prefix. The server is local and
headless: it starts its shared WebSocket service on the first image request,
then stays resident for later requests. It stops after ten idle minutes by
default.

Use these tools and defaults:

- `spriteloom_status` checks service state and does not start the service.
- `generate_sprite(prompt, width=64, height=64, variants=1, seed=None,
  palette=None, background="auto", output_dir=None)` creates a new sprite.
- `edit_sprite(image_path, prompt, preserve_palette=True, ...)` edits an
  existing image. Use the tool's optional width/height and output controls
  when the request needs them.
- `inpaint_sprite(image_path, mask_path, prompt, ...)` redraws only a mask.
- `rotate_sprite(image_path, subject, view, ..., symmetry=False)` creates an
  explicit view of the same subject.
- `list_generations(limit=20, output_dir=None)` lists saved runs.
- `stop_service()` releases the service only when the user asks to stop it or
  free GPU memory.

Keep one variant as the default unless the user asks for more. Do not retry
without a bounded reason. The service remains available between requests;
do not stop it after each call. On the measured 32 GB Apple M5 setup, an
8-step 512-pixel working request that produces a 64-pixel sprite takes about
17–21 seconds and uses about 12.3 GB of peak MLX allocation. Other backends
can differ, including for the same seed.

Preserve an existing palette by leaving `preserve_palette=True`. Set it to
`False` when the user asks for new colors. In an inpaint mask, white means
repaint and black means preserve. The mask must have the same dimensions as
the source. SpriteLoom writes a new composite PNG and clips the result to the
exact mask; it does not modify the original image.

Inspect the returned image preview. Check the requested subject, colors,
view, and transparent background. If a result fails a clear requirement,
make one focused correction and check again. Report any remaining limit.
Report the saved absolute PNG paths and seeds. Do not
treat an angle request as sprite animation: `rotate_sprite` creates one
requested view, while animation requires a separate user request.
