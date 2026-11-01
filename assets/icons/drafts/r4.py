"""Sediment icon round 4: r3 with HA blue #18bcf2 and much coarser, hard-edged lower sheets.

Usage: py -3 assets/icons/drafts/r4.py            writes concept-r4.svg, palette-r4.json, preview-r4.png
       py -3 assets/icons/drafts/r4.py --export   also writes the finals (see export())
(needs resvg-py and Pillow; every loop is bounded)

What changed against r3 (r3.py is imported for the shared geometry and the chart):
- Accent is #18bcf2 only: the Home Assistant logo blue (fill of home-assistant.io favicon.svg,
  frontend ha-color-primary-50), as the owner specified.
- The chart image is rendered at 256 x 256, then box-downsampled to MID x MID (16 x 16) for the
  middle sheet and LOW x LOW (8 x 8) for the bottom sheet. Neutral and accent parts are measured
  separately (coverage per cell), so a cell can keep the spike color.
- Cells are posterised: no cell below COVER_MIN coverage, one of two levels per color (solid when
  coverage is at least COVER_SOLID, half tone otherwise). No blur, no smooth grey.
- Cells are drawn with a hairline gap (GAP of a cell) so they read as separate blocks.
"""
import json
import pathlib
import shutil
import sys

import resvg_py
from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import palette as P  # noqa: E402
import r3  # noqa: E402
import render as R  # noqa: E402

ACCENT = "#18bcf2"
NEUTRAL, PAPER, TILE = r3.NEUTRAL, r3.PAPER, r3.TILE
MID, LOW = 12, 6
COVER_MIN = 0.12  # a cell with less coverage than this stays empty
COVER_SOLID = 0.45  # at or above this the cell takes the full color, below it the half tone
ACCENT_MIN = 0.10  # accent coverage that makes a cell an accent cell
GAP_UNITS = None  # r5: open gap per side in image units, so finer grids keep the same absolute gap
GAP = 0.07  # fraction of a cell left open on each side, so cells read as blocks


def mix(a, b, t):
    ra, rb = P.hex_to_rgb(a), P.hex_to_rgb(b)
    return P.rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def coverage(neutral, accent, n):
    """Per cell alpha coverage of the neutral parts and of the accent parts, on an n x n grid."""
    out = []
    for nn, aa in ((neutral, "none"), ("none", accent)):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {r3.IMG} {r3.IMG}" width="{r3.IMG}" height="{r3.IMG}">'
               f'{r3.chart(nn, aa)}</svg>')
        png = resvg_py.svg_to_bytes(svg_string=svg, width=r3.IMG, height=r3.IMG)
        import io
        img = Image.open(io.BytesIO(bytes(png))).convert("RGBA").getchannel("A")
        out.append(img.resize((n, n), Image.BOX))
    return out


def cells(neutral, accent, n, oy):
    cn, ca = coverage(neutral, accent, n)
    half = {neutral: mix(PAPER, neutral, 0.45), accent: mix(PAPER, accent, 0.5)}
    s, rows = r3.IMG / n, []
    pad = GAP if GAP_UNITS is None else GAP_UNITS / s
    for j in range(n):
        for i in range(n):
            an, aa = cn.getpixel((i, j)) / 255, ca.getpixel((i, j)) / 255
            if an + aa < COVER_MIN:
                continue
            base = accent if aa >= ACCENT_MIN else neutral
            cov = aa if base == accent else an
            fill = base if cov >= COVER_SOLID else half[base]
            x0, x1, y0, y1 = (i + pad) * s, (i + 1 - pad) * s, (j + pad) * s, (j + 1 - pad) * s
            pts = r3.poly([r3.at(oy, x0, y0), r3.at(oy, x1, y0), r3.at(oy, x1, y1), r3.at(oy, x0, y1)])
            rows.append(f'    <polygon points="{pts}" fill="{fill}"/>')
    return "\n".join(rows)


def concept_r4(neutral=NEUTRAL, accent=ACCENT, title="sediment", badge=True):
    y1, y2, y3 = r3.Y1, r3.Y1 + r3.STEP, r3.Y1 + 2 * r3.STEP
    rh = lambda oy: r3.poly([r3.at(oy, 0, 0), r3.at(oy, r3.IMG, 0), r3.at(oy, r3.IMG, r3.IMG), r3.at(oy, 0, r3.IMG)])
    paper = lambda oy: (f'  <polygon points="{rh(oy)}" fill="{PAPER}" stroke="{TILE}" stroke-width="22" stroke-linejoin="round" paint-order="stroke"/>\n'
                        f'  <polygon points="{rh(oy)}" fill="{PAPER}"/>\n')
    m = f"matrix({r3.DX / r3.IMG:.5f} {r3.DY / r3.IMG:.5f} {-r3.DX / r3.IMG:.5f} {r3.DY / r3.IMG:.5f} {r3.CX} {y1})"
    s = (P.HEAD.format(title) + P.TILE_RECT +
         f'  <!-- Bottom sheet: the chart image at {LOW} x {LOW} cells, hard edged, two tones per color -->\n' + paper(y3) +
         f'  <g>\n{cells(neutral, accent, LOW, y3)}\n  </g>\n'
         f'  <!-- Middle sheet: the same image at {MID} x {MID} cells -->\n' + paper(y2) +
         f'  <g>\n{cells(neutral, accent, MID, y2)}\n  </g>\n'
         '  <!-- Top sheet: the framed chart as vector, skewed onto the sheet plane -->\n' + paper(y1) +
         f'  <g transform="{m}">\n{r3.chart(neutral, accent)}\n  </g>\n')
    if badge:
        cx, cy, r = r3.DISC
        s += ('  <!-- Badge: MDI history (Apache 2.0), 20 percent of the tile, on a cut-out disc that removes the sheets behind it -->\n'
              f'  <circle cx="{cx}" cy="{cy}" r="{r}" fill="{TILE}"/>\n'
              f'  <path transform="translate({cx - 11.5 * r3.GLYPH_SCALE:.1f} {cy - 12 * r3.GLYPH_SCALE:.1f}) scale({r3.GLYPH_SCALE})" fill="{neutral}" d="{P.MDI_HISTORY}"/>\n')
    return s + '</svg>\n'


def checks():
    L, C, H = P.hex_to_oklch(ACCENT)
    return {
        "accent": {"hex": ACCENT, "source": "Home Assistant logo blue: fill in home-assistant.io source/images/favicon.svg, frontend ha-color-primary-50",
                   "oklch": [round(L, 3), round(C, 3), round(H, 1)],
                   "contrast_on_tile": round(P.contrast(ACCENT, TILE), 2),
                   "contrast_on_paper": round(P.contrast(ACCENT, PAPER), 2),
                   "vs_neutral": {"normal": round(P.oklab_de(ACCENT, NEUTRAL), 3), **P.cvd_de(ACCENT, NEUTRAL)},
                   "vs_paper": {"normal": round(P.oklab_de(ACCENT, PAPER), 3), **P.cvd_de(ACCENT, PAPER)},
                   "hue_gap_to_get_the_camera_6ec6ff": round(P.hue_gap(H, P.hex_to_oklch("#6ec6ff")[2]), 1),
                   "hue_gap_to_teal": round(P.hue_gap(H, P.hex_to_oklch("#4fd1c5")[2]), 1),
                   "simulated": {k: P.simulate(ACCENT, k) for k in P.MACHADO}},
        "neutral": {"hex": NEUTRAL, "contrast_on_tile": round(P.contrast(NEUTRAL, TILE), 2),
                    "contrast_on_paper": round(P.contrast(NEUTRAL, PAPER), 2)},
        "steps": {"top": "vector", "mid_cells": MID, "low_cells": LOW, "cover_min": COVER_MIN,
                  "cover_solid": COVER_SOLID, "accent_min": ACCENT_MIN, "gap": GAP},
    }


def text_cell(lines, ink, size=(330, 150)):
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k, line in enumerate(lines):
        d.text((0, 4 + 20 * k), line, fill=ink, font=R.font(15))
    return (img, "")


def preview(svg_r4):
    pal = checks()
    a = pal["accent"]
    r3svg = r3.concept_r3(NEUTRAL, "#009ac7")
    panels = []
    for theme, title, bg in R.PANELS:
        dark = theme == "dark"
        ink = (230, 230, 230, 255) if dark else (30, 30, 30, 255)
        rows = []
        for name, svg in (("r3  #009ac7", r3svg), ("r4  #18bcf2", svg_r4)):
            cells_ = [(R.render_svg(svg, s), str(s)) for s in (256, 128, 64, 32)]
            cells_.append((R.render_svg(svg, 64).resize((256, 256), Image.NEAREST), "64 x4"))
            cells_.append((R.render_svg(svg, 32).resize((256, 256), Image.NEAREST), "32 x8"))
            rows.append((name, cells_))
        if dark:
            base = R.render_svg(svg_r4, 96)
            sims = [(base, "normal 96")] + [(R.simulate_image(base, k), k[:6] + " 96") for k in P.MACHADO]
            nums = text_cell([
                f"#18bcf2 on tile {a['contrast_on_tile']}:1, on paper {a['contrast_on_paper']}:1",
                f"neutral on tile {pal['neutral']['contrast_on_tile']}:1",
                "accent vs neutral, OKLab (min 0.10):",
                f"  normal {a['vs_neutral']['normal']}, prot {a['vs_neutral']['protanopia']}",
                f"  deut {a['vs_neutral']['deuteranopia']}, trit {a['vs_neutral']['tritanopia']}",
                f"hue {a['oklch'][2]}, gap to #6ec6ff {a['hue_gap_to_get_the_camera_6ec6ff']} deg",
            ], ink)
            rows.append(("r4 colour-blind", sims + [nums]))
        panels.append(R.panel(theme, title, bg, rows))
    out = Image.new("RGB", (max(p.width for p in panels), sum(p.height for p in panels)))
    y = 0
    for p in panels:
        out.paste(p, (0, y))
        y += p.height
    out.save(HERE / "preview-r4.png")
    print(HERE / "preview-r4.png", out.size)
    return pal


def optimise(img, path):
    img.save(path, "PNG", optimize=True)
    print(path, img.size, path.stat().st_size, "bytes")


def export(svg):
    root = HERE.parents[2]
    icons, brand = root / "assets" / "icons", root / "custom_components" / "sediment" / "brand"
    brand.mkdir(exist_ok=True)
    final = svg.replace("<title>sediment</title>", "<title>sediment</title>", 1)
    (icons / "sediment.svg").write_bytes(final.encode("utf-8"))
    for size, path in ((512, icons / "sediment-512.png"), (128, icons / "sediment-128.png"),
                       (256, brand / "icon.png"), (512, brand / "icon@2x.png")):
        optimise(R.render_svg(final, size), path)


def main():
    svg = concept_r4()
    (HERE / "concept-r4.svg").write_bytes(svg.encode("utf-8"))
    pal = preview(svg)
    (HERE / "palette-r4.json").write_bytes((json.dumps(pal, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(pal["accent"]["vs_neutral"]), pal["accent"]["contrast_on_tile"], len(svg), "bytes svg")
    if "--export" in sys.argv[1:]:
        export(svg)


if __name__ == "__main__":
    main()
