"""Render the Sediment icon concepts to preview.png.

Usage (after py -3 -m pip install --user resvg-py Pillow and py -3 assets/icons/drafts/palette.py):
    py -3 assets/icons/drafts/render.py          preview.png, round 1
    py -3 assets/icons/drafts/render.py --r2     preview-r2.png, round 2

Every size is rendered from the SVG at that size, never downscaled. Two panels, one on the dark
Windows surface color (#202020), one on the light one (#f3f3f3), so the tile is judged on both.
The dark panel also shows each concept as seen under protanopia, deuteranopia and tritanopia
(Machado, Oliveira and Fernandes 2009, severity 1.0, applied per pixel to linear sRGB) and in
the alternative accent hues. Sediment has no tray icon, so there are no tray rows.
"""
import io
import json
import pathlib
import sys

import resvg_py
from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import palette  # noqa: E402

SIZES = (256, 128, 64, 32, 24, 16)
ZOOM = 8
GAP = 20
MIN_W = 1760
LABEL_W = 290
PANELS = (("dark", "dark surface #202020", (32, 32, 32)), ("light", "light surface #f3f3f3", (243, 243, 243)))


def render_svg(svg: str, size: int) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def font(size: int):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def simulate_image(img: Image.Image, kind: str) -> Image.Image:
    m = palette.MACHADO[kind]
    lut = [palette.to_linear(i / 255) for i in range(256)]
    out = Image.new("RGBA", img.size)
    src, dst = img.load(), out.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = src[x, y]
            lin = (lut[r], lut[g], lut[b])
            c = [palette.from_linear(min(max(sum(w * v for w, v in zip(row, lin)), 0), 1)) for row in m]
            dst[x, y] = (round(c[0] * 255), round(c[1] * 255), round(c[2] * 255), a)
    return out


def concept_cells(svg: str, simulated: bool):
    cells = [(render_svg(svg, s), str(s)) for s in SIZES]
    cells.append((render_svg(svg, 16).resize((16 * ZOOM,) * 2, Image.NEAREST), "16 x8"))
    if simulated:
        base = render_svg(svg, 96)
        for kind in palette.MACHADO:
            cells.append((simulate_image(base, kind), kind[:6] + " 96"))
    return cells


def panel(theme: str, title: str, bg, rows: list) -> Image.Image:
    cell_w = lambda im: max(im.width, 56)
    width = LABEL_W + max(sum(cell_w(i) + GAP for i, _ in cells) for _, cells in rows if cells) + GAP
    heights = [40 if cells is None else max(i.height for i, _ in cells) + 34 for _, cells in rows]
    img = Image.new("RGB", (max(width, MIN_W), 50 + sum(h + GAP for h in heights)), bg)
    draw = ImageDraw.Draw(img)
    ink = (230, 230, 230) if theme == "dark" else (30, 30, 30)
    draw.text((GAP, 14), title, fill=ink, font=font(22))
    y = 50
    for (label, cells), h in zip(rows, heights):
        if cells is None:
            draw.text((GAP, y + 8), label, fill=ink, font=font(24))
        else:
            for k, line in enumerate(label.split("\n")):
                draw.text((GAP, y + 10 + 26 * k), line, fill=ink, font=font(20 if k == 0 else 15))
            x = LABEL_W
            for im, caption in cells:
                img.paste(im, (x, y + (h - 34 - im.height) // 2), im)
                draw.text((x, y + h - 28), caption, fill=ink, font=font(15))
                x += cell_w(im) + GAP
        y += h + GAP
    return img


def comparison_row(label, items, ink_bg):
    """Side by side at 128, 64 and 32 px, one group per icon."""
    cells = []
    for name, svg in items:
        for size in (128, 64, 32):
            cells.append((render_svg(svg, size), f"{name} {size}" if size == 128 else str(size)))
    return (label, cells)


def main_r2() -> None:
    pal = json.loads((HERE / "palette.json").read_text(encoding="utf-8"))
    neutral, accent = pal["neutral"]["hex"], pal["accent"]["hex"]
    hues = [("284", accent)] + [(str(h), pal["variants"][k]["accent"]) for h, k in
                                ((134, "split-complementary 134"), (352, "extra dart sediment#6"))]
    b1 = palette.CONCEPTS["concept-b-chart"][2](neutral, accent, "b")
    r2 = {k: fn(neutral, accent, k) for k, (_, _, fn) in palette.CONCEPTS_R2.items()}
    compare = [("B", b1), ("B2 clock", r2["concept-b2-badge"]), ("B2 plain", r2["concept-b2-plain"]),
               ("A2 small", r2["concept-a2-badge"]), ("A2 none", r2["concept-a2-plain"])]
    panels = []
    for theme, title, bg in PANELS:
        dark = theme == "dark"
        rows = [("Side by side", None), comparison_row("B vs round 2", compare, bg)]
        rows.append(("Round 2 concepts", None))
        for key, (letter, name, fn) in palette.CONCEPTS_R2.items():
            rows.append((f"{letter}  {name}\n{accent}", concept_cells(r2[key], dark)))
        if dark:
            rows.append(("Accent hues 284, 134, 352 (96 px)", None))
            for key, (letter, name, fn) in palette.CONCEPTS_R2.items():
                cells = [(render_svg(fn(neutral, c, key), 96), f"{h} {c}") for h, c in hues]
                rows.append((f"{letter}  {name}", cells))
        panels.append(panel(theme, title, bg, rows))
    out = Image.new("RGB", (max(p.width for p in panels), sum(p.height for p in panels)))
    y = 0
    for p in panels:
        out.paste(p, (0, y))
        y += p.height
    target = HERE / "preview-r2.png"
    out.save(target)
    print(target, out.size)


def main() -> None:
    pal = json.loads((HERE / "palette.json").read_text(encoding="utf-8"))
    neutral, accent = pal["neutral"]["hex"], pal["accent"]["hex"]
    variants = [(k, v) for k, v in pal["variants"].items() if v.get("accent")]
    panels = []
    for theme, title, bg in PANELS:
        dark = theme == "dark"
        rows = []
        for key, (letter, name, fn) in palette.CONCEPTS.items():
            svg = fn(neutral, accent, key)
            rows.append((f"{letter}  {name}\n{accent}", concept_cells(svg, dark)))
        if dark:
            rows.append(("Accent variants (96 px)", None))
            for key, (letter, name, fn) in palette.CONCEPTS.items():
                cells = [(render_svg(fn(neutral, v["accent"], key), 96), f"{v['hue']} {v['accent']}") for _, v in variants]
                rows.append((f"{letter}  {name}", cells))
        panels.append(panel(theme, title, bg, rows))
    out = Image.new("RGB", (max(p.width for p in panels), sum(p.height for p in panels)))
    y = 0
    for p in panels:
        out.paste(p, (0, y))
        y += p.height
    target = HERE / "preview.png"
    out.save(target)
    print(target, out.size)


if __name__ == "__main__":
    main_r2() if "--r2" in sys.argv[1:] else main()
