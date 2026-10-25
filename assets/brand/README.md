# Usage Pulse brand

## The mark

A heartbeat trace of neon light, cyan on the left running to indigo and fuchsia on the right, passing through a slab of black obsidian glass with sharp facets. It is the pulse in the name, rendered rather than drawn, so it reads as a physical object at every size and looks like nothing else in the Claude Code plugin lists.

`emblem.png` was rendered locally on 2026-10-25 with **Z-Image-Turbo** (Apache 2.0) in ComfyUI 0.41 on an RTX 5090: 8 steps, `res_multistep` sampler, cfg 1, 1024×1024, seed `8003`. Prompt:

> A premium brand emblem: a heartbeat pulse waveform made of glowing neon light, cyan fading to indigo and magenta, running through a thick slab of black obsidian glass, carved from black obsidian glass with razor-sharp bevelled facets, light leaking through the glass, floating on a pure black background, volumetric light haze, cinematic studio lighting, octane render, ultra detailed, centered, no text, no letters

Nine candidates were rendered and compared on a contact sheet: three stacked-bar meters (the old logo's motif; one carried stray text), three heartbeat traces and three gauge dials (generic, and one grew numbers). Among the traces, seed 8003 has the cleanest facets, the classic heartbeat shape, and a line that leaves the glass towards the wordmark.

## Derived files

All built by `python scripts/make_hero.py` from the emblem, so they can be regenerated pixel for pixel:

| File | Size | Use |
|---|---|---|
| `hero.png` | 1600×600, rounded corners, transparent outside | README header, shown at 800 px wide |
| `social-preview.png` | 1280×640 | GitHub repository social preview (uploaded by hand in Settings) |
| `icon.png` | 512×512, rounded square | Plugin listings and avatars |

The demo pictures in `assets/*.gif` are a different pipeline: `scripts/readme_assets.py` records them from the real status line.

## Type and colour

- Wordmark: **Unbounded** ExtraBold (SIL Open Font License), white to cyan `#22d3ee` to indigo `#818cf8` to fuchsia `#e879f9`, the gradient the project has used since its first logo, over a soft indigo glow.
- Tagline and labels: **JetBrains Mono** (SIL OFL).
- Background `#06070b`; text `#f8fafc`, muted `#cbd5e1`.
- Both fonts are vendored in `fonts/` as static instances, with their licence files.
