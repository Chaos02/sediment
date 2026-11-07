"""Build the transparent Home Assistant icons from sediment.svg.

Usage (py -3 -m pip install --user resvg-py Pillow first):
    py -3 assets/icons/bare.py            writes the two SVGs and the brand PNGs
    py -3 assets/icons/bare.py --preview  also writes drafts/preview-bare.png

Home Assistant shows integration icons on its own background, so the icon has no tile. The gaps
between the sheets and around the badge, which sediment.svg paints in the tile color, are real
cut-outs here (SVG masks). Two variants: `icon` for the light theme keeps the dark sheets,
`dark_icon` for the dark theme uses lighter sheets, because dark sheets vanish on a dark card.

Every PNG is rendered from the SVG at its own size, never downscaled.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import sys

from PIL import Image, ImageDraw
import resvg_py

HERE = Path(__file__).resolve().parent
BRAND = HERE.parent.parent / "custom_components" / "sediment" / "brand"
TILE = "#1f2933"
SHEET = "#29363f"
SHEET_DARK_THEME = "#46576a"
GAP = 22  # width of the stroke the tile version paints around each sheet
NEUTRAL = "#e2e5e8"
BADGE_ON_LIGHT = "#1f2933"  # the clock sits on the page itself, so it needs contrast there
VARIANTS = {"": (SHEET, BADGE_ON_LIGHT), "dark_": (SHEET_DARK_THEME, NEUTRAL)}
SVG_NAMES = {"": "sediment-transparent.svg", "dark_": "sediment-transparent-dark.svg"}


def slices(svg: str) -> dict[str, str]:
    """The text after each comment marker, up to the next marker."""
    marks = {
        "bottom": "<!-- Bottom sheet",
        "middle": "<!-- Middle sheet",
        "top": "<!-- Top sheet",
        "badge": "<!-- Badge",
    }
    pos = {k: svg.index(m) for k, m in marks.items()}
    end = svg.rindex("</svg>")
    order = sorted(pos, key=pos.get)
    out = {}
    for i, key in enumerate(order):
        stop = pos[order[i + 1]] if i + 1 < len(order) else end
        out[key] = svg[pos[key] : stop]
    return out


def sheet_polygon(part: str) -> str:
    return re.search(r'<polygon points="([^"]+)"', part).group(1)


def build(svg: str, sheet_color: str, badge_color: str) -> str:
    parts = slices(svg)
    badge_circle = re.search(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', parts["badge"])
    cx, cy, r = badge_circle.groups()
    polys = {k: sheet_polygon(parts[k]) for k in ("bottom", "middle", "top")}

    def hole(points: str) -> str:
        return (
            f'<polygon points="{points}" fill="#000" stroke="#000" stroke-width="{GAP}" '
            'stroke-linejoin="round"/>'
        )

    badge_hole = f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#000"/>'
    covers = {
        "bottom": [hole(polys["middle"]), hole(polys["top"]), badge_hole],
        "middle": [hole(polys["top"]), badge_hole],
        "top": [badge_hole],
    }
    masks = []
    for key, holes in covers.items():
        masks.append(
            f'<mask id="m-{key}" maskUnits="userSpaceOnUse" x="0" y="0" width="512" height="512">'
            '<rect width="512" height="512" fill="#fff"/>' + "".join(holes) + "</mask>"
        )
    body = []
    for key in ("bottom", "middle", "top"):
        part = parts[key]
        comment, rest = part.split("-->", 1)
        lines = rest.strip().splitlines()
        # the first line is the polygon with the tile colored stroke; the mask replaces it
        assert "stroke=" in lines[0] and 'paint-order="stroke"' in lines[0], lines[0][:80]
        body.append(
            f'{comment}-->\n  <g mask="url(#m-{key})">\n' + "\n".join(lines[1:]) + "\n  </g>"
        )
    badge = parts["badge"]
    comment, rest = badge.split("-->", 1)
    badge_lines = [
        ln.replace(f'fill="{NEUTRAL}"', f'fill="{badge_color}"')
        for ln in rest.strip().splitlines()
        if "<circle" not in ln
    ]
    body.append(comment.split(", on a cut-out")[0] + " -->\n  " + "\n".join(badge_lines))
    head = svg[: svg.index("<!-- Tile")]
    out = head + "<defs>" + "".join(masks) + "</defs>\n  " + "\n  ".join(body) + "\n</svg>\n"
    out = out.replace(SHEET, sheet_color)
    assert TILE not in out.replace(badge_color, ""), "a tile colored shape is left"
    return out


def render(svg: str, size: int) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def preview(svgs: dict[str, str]) -> Image.Image:
    sizes = (256, 64, 32)
    pad = 24
    width = pad + sum(s + pad for s in sizes)
    sheet = Image.new("RGB", (width, 2 * (256 + 2 * pad)), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for row, (prefix, bg) in enumerate((("", (243, 243, 243)), ("dark_", (28, 28, 28)))):
        y0 = row * (256 + 2 * pad)
        draw.rectangle([0, y0, width, y0 + 256 + 2 * pad], fill=bg)
        x = pad
        for size in sizes:
            img = render(svgs[prefix], size)
            sheet.paste(img, (x, y0 + pad), img)
            x += size + pad
    return sheet


def main() -> None:
    src = (HERE / "sediment.svg").read_text(encoding="utf-8")
    svgs = {prefix: build(src, *colors) for prefix, colors in VARIANTS.items()}
    for prefix, svg in svgs.items():
        with (HERE / SVG_NAMES[prefix]).open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(svg)
        render(svg, 256).save(BRAND / f"{prefix}icon.png", optimize=True)
        render(svg, 512).save(BRAND / f"{prefix}icon@2x.png", optimize=True)
    if "--preview" in sys.argv[1:]:
        preview(svgs).save(HERE / "drafts" / "preview-bare.png")
    print("written:", ", ".join(sorted(p.name for p in BRAND.iterdir())))


if __name__ == "__main__":
    main()
