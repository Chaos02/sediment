"""Sediment icon round 5: r4 with finer lower sheets.

Usage: py -3 assets/icons/drafts/r5.py [--export]   (bounded, runs once)

The owner found r4 too coarse. The middle sheet goes from 12 x 12 to MID x MID cells and the bottom
sheet from 6 x 6 to LOW x LOW cells, so the bottom sheet is only slightly coarser than the old middle
sheet. Everything else is r4 (hard edged tiled cells, two tones per color, accent #18bcf2). The gap
between cells is set in image units (GAP_UNITS per side) instead of as a fraction of a cell, so the
finer middle sheet keeps a gap of the same size and does not fall apart into haze at 64 px.
Writes concept-r5.svg, palette-r5.json, preview-r5.png; with --export also the finals.
"""
import json
import pathlib
import sys

from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import palette as P  # noqa: E402
import r4  # noqa: E402
import render as R  # noqa: E402

MID, LOW, GAP_UNITS = 18, 10, 1.3
R4_STEPS = (12, 6, 0.07, None)


def build(mid, low, gap, gap_units):
    r4.MID, r4.LOW, r4.GAP, r4.GAP_UNITS = mid, low, gap, gap_units
    return r4.concept_r4()


def main():
    old = build(*R4_STEPS)
    new = build(MID, LOW, r4.GAP, GAP_UNITS)
    (HERE / "concept-r5.svg").write_bytes(new.encode("utf-8"))
    pal = r4.checks()
    pal["steps"].update({"mid_cells": MID, "low_cells": LOW, "gap_units_per_side": GAP_UNITS})
    (HERE / "palette-r5.json").write_bytes((json.dumps(pal, indent=2) + "\n").encode("utf-8"))
    panels = []
    for theme, title, bg in R.PANELS:
        rows = []
        for name, svg in (("r4  12 / 6", old), (f"r5  {MID} / {LOW}", new)):
            cells = [(R.render_svg(svg, s), str(s)) for s in (256, 128, 64, 32)]
            cells.append((R.render_svg(svg, 64).resize((256, 256), Image.NEAREST), "64 x4"))
            cells.append((R.render_svg(svg, 32).resize((256, 256), Image.NEAREST), "32 x8"))
            rows.append((name, cells))
        if theme == "dark":
            base = R.render_svg(new, 96)
            rows.append(("r5 colour-blind", [(base, "normal 96")] + [(R.simulate_image(base, k), k[:6] + " 96") for k in P.MACHADO]))
        panels.append(R.panel(theme, title, bg, rows))
    out = Image.new("RGB", (max(p.width for p in panels), sum(p.height for p in panels)))
    y = 0
    for p in panels:
        out.paste(p, (0, y))
        y += p.height
    out.save(HERE / "preview-r5.png")
    print(HERE / "preview-r5.png", out.size, len(new), "bytes svg")
    if "--export" in sys.argv[1:]:
        r4.export(new)


if __name__ == "__main__":
    main()
