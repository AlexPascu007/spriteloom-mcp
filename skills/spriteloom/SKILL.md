---
name: spriteloom
description: Generate and edit pixel-art sprites with a local Spriteloom (FLUX.2 Klein) server via the spriteloom MCP tools. Use when asked to make, edit, vary, or upscale sprites, game items, characters or animation frames.
---

# Spriteloom sprites

Tools (MCP server `spriteloom`): `spriteloom_status`, `spriteloom_generate`,
`spriteloom_edit`, `spriteloom_inpaint`. Each call writes PNGs to
`~/spriteloom-output/<timestamp>_<id>/variant_N.png` and returns the paths and seeds.

## Workflow

1. `spriteloom_status` first. It must say `model: ready`. If unreachable, the
   Spriteloom app is not running or the SSH tunnel is down (see README).
2. `spriteloom_generate` with 4 variants at the target size to explore.
3. Look at the results. The outputs are tiny, so build a contact sheet scaled
   up with nearest-neighbour (e.g. 4x) before judging them.
4. Refine the best variant with `spriteloom_edit` (pass its path as `image_path`).
5. To upscale, run `spriteloom_edit` on the small sprite with a bigger
   width/height (64 -> 128) and a prompt that says "same sprite, more detail".

## Prompting lessons

- Describe the object, material and pose: "fantasy sword, blade of rainbow
  iridescent crystal, faceted prismatic shards, simple hilt, diagonal".
- Edits: say what to change AND what to keep ("keep the blade exactly the same").
- "Make X black" alone produces a flat silhouette with no detail. Ask for
  texture and accents instead ("black leather-wrapped handle, silver pommel").
- Higher resolution (128) adds real detail such as grip wrapping; 64 loses it.
- Expect about 1 in 4 variants to be flawed. Common failures: leftover
  background block (use `background: "remove"`), grungy texture, blade
  length drifting between edits. Reuse a seed to reproduce a result.

## Parameters

- `width`/`height` 1-4096 (final sprite size, background auto-removed and
  palette-snapped), `variants` 1-8, `seed` 0..2^32-1.
- `background`: `auto`, `remove`, `keep`. `symmetry`: mirror left/right.
- `palette`: `[[r,g,b],...]` (max 256) pins colors; edits default to the input's palette.
- `spriteloom_inpaint`: mask PNG same size as the image, white = repaint,
  black = keep. Output is a transparent layer with only the new content.
