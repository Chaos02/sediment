# Sediment icon drafts

Three concepts for the Sediment app icon. Nothing here is final; the owner picks first. No tray icon is needed (Home Assistant integration).

| File | Shows | MDI glyphs |
| --- | --- | --- |
| `concept-a-layers.svg` | Three stacked layers, the newest in the accent, with a time arrow as badge. | `layers-triple`, `history` |
| `concept-b-chart.svg` | A line chart on a time axis. Old samples are few and far apart, recent ones dense. The old spike survives in the accent. | none (hand drawn) |
| `concept-c-strata.svg` | Strata in 16, 8, 4 and 1 cells per row, thicker with depth, with one accent spike kept through every row. | none (hand drawn) |

`preview.png` shows every concept at 256, 128, 64, 32, 24 and 16 px (plus 16 px at 8x) on a dark and a light surface, under three colour-blind simulations, and in the alternative accent hues.

## Colour rule

`palette.py` derives every colour by rule and writes `palette.json` with all numbers. The neutral is `#e2e5e8` (OKLCH 0.92, 0.006, 250). The accent hue is a dart: SHA-256 of the seed `sediment`, mod 360. Seeds `sediment`, `#2` and `#3` gave 62, 60 and 187, which sit within 25 degrees of amber or teal, so `sediment#4` (hue 284) is used. Accent `#b3b3fe`, 7.59:1 on the tile. Every pair must be at least 0.10 apart in OKLab under protanopia, deuteranopia and tritanopia (Machado 2009, severity 1.0).

## Regenerate

`py -3 assets/icons/drafts/palette.py`, then `py -3 assets/icons/drafts/render.py` (needs `py -3 -m pip install --user resvg-py Pillow`).

## License of the glyphs

Glyph path data from [Material Design Icons](https://pictogrammers.com/library/mdi/) by Pictogrammers, unchanged except for scale, position and splitting `layers-triple` at its sub-paths, under the Apache License 2.0:

> Copyright (c) Pictogrammers. Licensed under the Apache License, Version 2.0. You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

Source: https://github.com/Templarian/MaterialDesign (`svg/<name>.svg`, 24x24 viewBox).

## Round 2

| File | Shows | MDI glyphs |
| --- | --- | --- |
| `concept-b2-badge.svg`, `concept-b2-plain.svg` | Concept B with time running left to right: a few kept points joined by long segments on the old side (spike in the accent), a dense jagged line on the recent side. With and without a clock badge. | `history` (badge only) |
| `concept-a2-badge.svg`, `concept-a2-plain.svg` | Three isometric sheets. The top sheet is an outline with a line graph skewed onto its plane (spike in the accent). The sheets below are cell grids, 8 x 8 and then 4 x 4. With a small clock badge and without. | `history` (badge only) |

`preview-r2.png` has a side-by-side row of B, B2 (clock), B2 (plain), A2 (small clock) and A2 (none) at 128, 64 and 32 px, then each round 2 concept at every size, under colour-blind simulation, and in hues 284, 134 and 352. Run `py -3 assets/icons/drafts/render.py --r2` to rebuild it.

## Round 3

`concept-r3.svg`: the B2 chart closed to a full frame (thin top and right lines) as the top sheet of a three sheet stack. The middle sheet is the same image halved once, the bottom sheet halved twice, shown pixelated. MDI `history` at 20 percent of the tile sits bottom right on a cut-out disc. Accent: Home Assistant blue `#009ac7` (the frontend `--primary-color`). `palette-r3.json` has the checks, `preview-r3.png` the sheet. Rebuild with `py -3 assets/icons/drafts/r3.py`.
