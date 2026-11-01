"""Sediment icon round 3: the B2 chart as the top sheet of a stack, thinned twice for the sheets below.

Usage: py -3 assets/icons/drafts/r3.py   (needs resvg-py and Pillow; runs once, every loop is bounded)

Writes concept-r3.svg (default accent), palette-r3.json and preview-r3.png.

Build, as the owner specified:
1. The sheet image is the B2 plain chart (no clock) closed to a full frame: the thick left and bottom
   axes of B2, plus a thin top line and a thin right line. It is drawn on a 256 x 256 image grid.
2. Top sheet: that image as vector, mapped onto an isometric rhombus.
3. Middle sheet: the image rendered at BASE x BASE (64 x 64) and downsampled by 2 (box filter on
   premultiplied pixels) to 32 x 32. Bottom sheet: downsampled by 2 again, to 16 x 16. Both are drawn
   as one skewed polygon per non-empty pixel, so nothing is smoothed when it is scaled up.
4. The stack fills 95 percent of the tile width.
5. MDI history at 20 percent of the tile, bottom right, on a tile colored cut-out disc.
6. Accent: Home Assistant blue (the frontend --primary-color, see ACCENTS).
"""
import io
import json
import math
import pathlib
import sys

import resvg_py
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import palette as P  # noqa: E402
import render as R  # noqa: E402

TILE, NEUTRAL = P.TILE, "#e2e5e8"
PAPER = "#29363f"  # the sheets: the tile color lifted a little, so a sheet reads as paper on the tile
# Home Assistant blues. Sources: home-assistant/frontend src/resources/theme/color/color.globals.ts
# (--primary-color: var(--ha-color-primary-40), core.globals.ts ha-color-primary-40: #009ac7;
# --light-blue-color: #03a9f4) and home-assistant.io source/images/favicon.svg (logo fill #18BCF2).
ACCENTS = {
    "frontend primary-color": "#009ac7",
    "logo blue": "#18bcf2",
    "classic light-blue": "#03a9f4",
}
SIB_GTC = "#6ec6ff"
BASE = 64  # raster size of the sheet image before the two halvings
IMG = 256  # the sheet image grid
DX, STEP = 243, 140  # half width of a sheet (95 percent of 512 over 2) and offset between sheets
DY = 100  # a flatter view than isometric (about 22 degrees), so more of each lower sheet shows
Y1 = round((512 - (2 * DY + 2 * STEP)) / 2, 1)
CX = 256
DISC = (432, 432, 64)
GLYPH_SCALE = 4.86  # the history glyph is about 21 units wide: 21 * 4.86 = 102 px, 20 percent of the tile


def chart(neutral, accent):
    """The framed B2 chart on the 256 grid, transparent background. Returns SVG elements."""
    X = lambda x: 40 + (x - 134) * 0.66
    Y = lambda y: 16 + (y - 100) * 0.7
    pts = lambda seq: " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in seq)
    dense = [(306, 326), (319, 300), (332, 330), (345, 292), (358, 322), (371, 284), (384, 314), (397, 276), (410, 306)]
    e = [
        f'<path d="M16 16V240H240" fill="none" stroke="{neutral}" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>',
        f'<path d="M16 16H240V240" fill="none" stroke="{neutral}" stroke-width="4"/>',
        f'<polyline points="{pts([(134, 342), (178, 322), (206, 316)])}" fill="none" stroke="{neutral}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>',
        f'<polyline points="{pts([(270, 316), (306, 326)])}" fill="none" stroke="{neutral}" stroke-width="12" stroke-linecap="round"/>',
    ]
    for x, y in ((134, 342), (178, 322), (306, 326)):
        e.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="10" fill="{neutral}"/>')
    e += [
        f'<polyline points="{pts([(206, 316), (238, 150), (270, 316)])}" fill="none" stroke="{accent}" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>',
        f'<circle cx="{X(238):.1f}" cy="{Y(150):.1f}" r="14" fill="{accent}"/>',
        f'<circle cx="{X(206):.1f}" cy="{Y(316):.1f}" r="9" fill="{accent}"/>',
        f'<circle cx="{X(270):.1f}" cy="{Y(316):.1f}" r="9" fill="{accent}"/>',
        f'<polyline points="{pts(dense)}" fill="none" stroke="{neutral}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>',
    ]
    return "\n".join(e)


def sheet_image(neutral, accent, size):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {IMG} {IMG}" width="{size}" height="{size}">{chart(neutral, accent)}</svg>'
    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def halve(img):
    """Downsample by 2 with a box filter on premultiplied pixels."""
    return img.convert("RGBa").resize((img.width // 2, img.height // 2), Image.BOX).convert("RGBA")


def at(oy, x, y):
    """Image grid point (x right, y down) to the screen, on the sheet whose top vertex is at (CX, oy)."""
    u, v = x / IMG, y / IMG
    return (CX + (u - v) * DX, oy + (u + v) * DY)


def poly(pts):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def pixel_polygons(img, oy):
    n, s, grow = img.width, IMG / img.width, 0.5
    out = []
    for j in range(n):
        for i in range(n):
            r, g, b, a = img.getpixel((i, j))
            if a < 10:
                continue
            x0, x1, y0, y1 = i * s - grow, (i + 1) * s + grow, j * s - grow, (j + 1) * s + grow
            pts = poly([at(oy, x0, y0), at(oy, x1, y0), at(oy, x1, y1), at(oy, x0, y1)])
            op = "" if a >= 250 else f' fill-opacity="{a / 255:.2f}"'
            out.append(f'    <polygon points="{pts}" fill="#{r:02x}{g:02x}{b:02x}"{op}/>')
    return "\n".join(out)


def concept_r3(neutral, accent, title="sediment concept R3", badge=True):
    base = sheet_image(neutral, accent, BASE)
    mid = halve(base)
    low = halve(mid)
    y2, y3 = Y1 + STEP, Y1 + 2 * STEP
    rh = lambda oy: poly([at(oy, 0, 0), at(oy, IMG, 0), at(oy, IMG, IMG), at(oy, 0, IMG)])
    paper = lambda oy: (f'  <polygon points="{rh(oy)}" fill="{PAPER}" stroke="{TILE}" stroke-width="22" stroke-linejoin="round" paint-order="stroke"/>\n'
                        f'  <polygon points="{rh(oy)}" fill="{PAPER}"/>\n')
    m = f"matrix({DX / IMG:.5f} {DY / IMG:.5f} {-DX / IMG:.5f} {DY / IMG:.5f} {CX} {Y1})"
    s = (P.HEAD.format(title) + P.TILE_RECT +
         f'  <!-- Bottom sheet: the chart image at {BASE} px, halved twice (16 x 16), one polygon per pixel -->\n' + paper(y3) +
         f'  <g>\n{pixel_polygons(low, y3)}\n  </g>\n'
         f'  <!-- Middle sheet: the chart image halved once (32 x 32) -->\n' + paper(y2) +
         f'  <g>\n{pixel_polygons(mid, y2)}\n  </g>\n'
         '  <!-- Top sheet: the framed chart as vector, skewed onto the sheet plane -->\n' + paper(Y1) +
         f'  <g transform="{m}">\n{chart(neutral, accent)}\n  </g>\n')
    if badge:
        cx, cy, r = DISC
        s += ('  <!-- Badge: MDI history (Apache 2.0), 20 percent of the tile, on a cut-out disc that removes the sheets behind it -->\n'
              f'  <circle cx="{cx}" cy="{cy}" r="{r}" fill="{TILE}"/>\n'
              f'  <path transform="translate({cx - 11.5 * GLYPH_SCALE:.1f} {cy - 12 * GLYPH_SCALE:.1f}) scale({GLYPH_SCALE})" fill="{neutral}" d="{P.MDI_HISTORY}"/>\n')
    return s + '</svg>\n'


def hue(h):
    return P.hex_to_oklch(h)[2]


def checks():
    out = {}
    for name, hx in ACCENTS.items():
        L, C, H = P.hex_to_oklch(hx)
        out[name] = {
            "hex": hx, "oklch": [round(L, 3), round(C, 3), round(H, 1)],
            "contrast_on_tile": round(P.contrast(hx, TILE), 2),
            "contrast_on_paper": round(P.contrast(hx, PAPER), 2),
            "vs_neutral": {"normal": round(P.oklab_de(hx, NEUTRAL), 3), **P.cvd_de(hx, NEUTRAL)},
            "vs_paper": {"normal": round(P.oklab_de(hx, PAPER), 3), **P.cvd_de(hx, PAPER)},
            "hue_gap_to_get_the_camera_6ec6ff": round(P.hue_gap(H, hue(SIB_GTC)), 1),
            "hue_gap_to_teal": round(P.hue_gap(H, hue("#4fd1c5")), 1),
            "hue_gap_to_amber": round(P.hue_gap(H, hue("#f6ad55")), 1),
            "simulated": {k: P.simulate(hx, k) for k in P.MACHADO},
        }
    out["neutral"] = {"hex": NEUTRAL, "contrast_on_tile": round(P.contrast(NEUTRAL, TILE), 2),
                      "contrast_on_paper": round(P.contrast(NEUTRAL, PAPER), 2)}
    out["paper"] = {"hex": PAPER, "contrast_on_tile": round(P.contrast(PAPER, TILE), 2)}
    out["geometry"] = {"dx": DX, "dy": DY, "step": STEP, "stack_width_pct": round(2 * DX / 5.12, 1),
                       "stack_height_pct": round((2 * DY + 2 * STEP) / 5.12, 1),
                       "base_px": BASE, "mid_px": BASE // 2, "low_px": BASE // 4}
    return out


def main():
    ac = list(ACCENTS.values())
    svg = concept_r3(NEUTRAL, ac[0])
    (HERE / "concept-r3.svg").write_bytes(svg.encode("utf-8"))
    pal = checks()
    (HERE / "palette-r3.json").write_bytes((json.dumps(pal, indent=2) + "\n").encode("utf-8"))

    variants = {n: concept_r3(NEUTRAL, h) for n, h in ACCENTS.items()}
    compare = []
    for n, v in variants.items():
        for size in (128, 64, 32):
            compare.append((R.render_svg(v, size), f"{ACCENTS[n]} {size}" if size == 128 else str(size)))
    plain = P.CONCEPTS_R2["concept-b2-plain"][2](NEUTRAL, ac[0], "b2")
    for size in (128, 64, 32):
        compare.append((R.render_svg(plain, size), f"B2 {size}" if size == 128 else str(size)))
    panels = []
    for theme, title, bg in R.PANELS:
        dark = theme == "dark"
        rows = [("Side by side: HA primary, logo blue, classic, B2", None), ("R3", compare),
                ("Size ladder", None), (f"R3\n{ac[0]}", R.concept_cells(variants["frontend primary-color"], dark))]
        if dark:
            rows.append(("Accent variants (128 px)", None))
            rows.append(("R3", [(R.render_svg(v, 128), f"{ACCENTS[n]}") for n, v in variants.items()]))
        panels.append(R.panel(theme, title, bg, rows))
    W = max(p.width for p in panels)
    out = Image.new("RGB", (W, sum(p.height for p in panels)))
    y = 0
    for p in panels:
        out.paste(p, (0, y))
        y += p.height
    out.save(HERE / "preview-r3.png")
    print(HERE / "preview-r3.png", out.size, len(svg), "bytes svg")
    print(json.dumps({k: pal[k] for k in ACCENTS}, indent=1))
    print(pal["geometry"])


if __name__ == "__main__":
    main()
