"""Derive the Sediment accent colors by rule and write the three concept SVGs.

Usage: py -3 assets/icons/drafts/palette.py   (standard library only)

Rule, same as get-the-camera, step by step:
1. Dart. hue = int(sha256(seed).hexdigest(), 16) mod 360, seed "sediment", then
   "sediment#2", "sediment#3" and so on. A hue within 25 degrees (OKLCH hue) of an
   accent already used in the family is skipped. Used accents: teal #4fd1c5 and
   amber #f6ad55 (marinated-updates), blue #6ec6ff (get-the-camera, final icon).
2. Accent lightness and chroma are the mean of the three used accents in OKLCH, with
   chroma lowered until the color fits sRGB. If the WCAG contrast on the tile #1f2933
   is below 4.5:1, lightness is raised.
3. Neutral base: OKLCH L 0.92, C 0.006, hue 250 (the tile's hue), which is #e2e5e8.
4. Color-blind check. Accent and neutral are simulated for protanopia, deuteranopia and
   tritanopia (Machado, Oliveira and Fernandes 2009, severity 1.0, linear sRGB). The
   distance is the Euclidean distance in OKLab and must be at least 0.10 under all
   three. If it is not, accent lightness is moved in 0.02 steps.
5. Variants: the dart, complementary (h+180), split-complementary (h+150, h+210),
   triadic (h+120, h+240) and two more darts. Partners within 25 degrees of a used
   accent are dropped.
Writes palette.json and concept-a-layers.svg, concept-b-chart.svg, concept-c-strata.svg.
"""
import hashlib
import json
import math
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
SEED = "sediment"
TILE = "#1f2933"
SIBLINGS = {
    "teal (marinated-updates, bestbefore)": "#4fd1c5",
    "amber (marinated-updates, marinade)": "#f6ad55",
    "blue (get-the-camera)": "#6ec6ff",
}
# Hues of rejected get-the-camera drafts. Not binding, reported only.
DRAFT_HUES = {"grape D3": 281, "D-round 41": 41, "D-round 131": 131, "D-round 101": 101}
MIN_HUE_GAP = 25.0
MIN_CONTRAST = 4.5
MIN_DE = 0.10
NEUTRAL_LCH = (0.92, 0.006, 250.0)
EXTRA_DARTS = 2

MACHADO = {  # severity 1.0, linear sRGB
    "protanopia": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deuteranopia": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritanopia": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}


def hex_to_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def rgb_to_hex(rgb):
    return "#" + "".join(f"{round(min(max(c, 0), 1) * 255):02x}" for c in rgb)


def to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def from_linear(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def linear_to_oklab(r, g, b):
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def oklab_to_linear(L, a, b):
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def hex_to_oklch(h):
    L, a, b = linear_to_oklab(*(to_linear(c) for c in hex_to_rgb(h)))
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def oklch_to_linear(L, C, H):
    return oklab_to_linear(L, C * math.cos(math.radians(H)), C * math.sin(math.radians(H)))


def in_gamut(lin, eps=1e-6):
    return all(-eps <= c <= 1 + eps for c in lin)


def oklch_to_hex(L, C, H):
    """Lower chroma until the color fits sRGB, then return hex."""
    while C > 0 and not in_gamut(oklch_to_linear(L, C, H)):
        C -= 0.001
    return rgb_to_hex(from_linear(min(max(c, 0), 1)) for c in oklch_to_linear(L, C, H))


def luminance(h):
    r, g, b = (to_linear(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    x, y = sorted((luminance(a), luminance(b)))
    return (y + 0.05) / (x + 0.05)


def simulate(h, kind):
    lin = [to_linear(c) for c in hex_to_rgb(h)]
    out = [sum(m * c for m, c in zip(row, lin)) for row in MACHADO[kind]]
    return rgb_to_hex(from_linear(min(max(c, 0), 1)) for c in out)


def oklab_de(a, b):
    p = linear_to_oklab(*(to_linear(c) for c in hex_to_rgb(a)))
    q = linear_to_oklab(*(to_linear(c) for c in hex_to_rgb(b)))
    return math.dist(p, q)


def cvd_de(a, b):
    return {k: round(oklab_de(simulate(a, k), simulate(b, k)), 3) for k in MACHADO}


def hue_gap(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def make_accent(L, C, H, neutral):
    """Accent at hue H: contrast floor on the tile, then CVD distance to the neutral."""
    h = oklch_to_hex(L, C, H)
    while contrast(h, TILE) < MIN_CONTRAST and L < 0.98:
        L += 0.005
        h = oklch_to_hex(L, C, H)
    L0, note = L, None
    if min(cvd_de(neutral, h).values()) < MIN_DE:
        note = "FAILED: no lightness reaches the threshold"
        for step in (-0.02, 0.02):
            L1 = L0
            while 0.3 < L1 + step < 0.98:
                L1 += step
                cand = oklch_to_hex(L1, C, H)
                if contrast(cand, TILE) < MIN_CONTRAST:
                    break
                if min(cvd_de(neutral, cand).values()) >= MIN_DE:
                    return cand, f"lightness moved from {L0:.3f} to {L1:.3f}"
    return h, note


def describe(hexv, neutral, siblings_oklch):
    return {
        "hex": hexv, "oklch": [round(x, 3) for x in hex_to_oklch(hexv)],
        "contrast_on_tile": round(contrast(hexv, TILE), 2),
        "vs_neutral": {"normal": round(oklab_de(hexv, neutral), 3), **cvd_de(hexv, neutral)},
        "hue_gap_to_siblings": {k: round(hue_gap(hex_to_oklch(hexv)[2], v[2]), 1) for k, v in siblings_oklch.items()},
        "simulated": {k: simulate(hexv, k) for k in MACHADO},
    }


# --- Geometry --------------------------------------------------------------------------
# Glyph path data from Material Design Icons (Apache 2.0), copied unchanged.
MDI_LAYERS_TRIPLE = ("M12 0L3 7L4.63 8.27L12 14L19.36 8.27L21 7L12 0"
                     "M19.37 10.73L12 16.47L4.62 10.74L3 12L12 19L21 12L19.37 10.73"
                     "M19.37 15.73L12 21.47L4.62 15.74L3 17L12 24L21 17L19.37 15.73Z")
# The three sub-paths above, split at their M commands so the top layer can take the accent.
MDI_LAYERS_TRIPLE_PARTS = ("M12 0L3 7L4.63 8.27L12 14L19.36 8.27L21 7L12 0Z",
                           "M19.37 10.73L12 16.47L4.62 10.74L3 12L12 19L21 12L19.37 10.73Z",
                           "M19.37 15.73L12 21.47L4.62 15.74L3 17L12 24L21 17L19.37 15.73Z")
MDI_HISTORY = ("M13.5,8H12V13L16.28,15.54L17,14.33L13.5,12.25V8M13,3A9,9 0 0,0 4,12H1L4.96,16.03L9,12H6A7,7 0 0,1 13,5"
               "A7,7 0 0,1 20,12A7,7 0 0,1 13,19C11.07,19 9.32,18.21 8.06,16.94L6.64,18.36C8.27,20 10.5,21 13,21"
               "A9,9 0 0,0 22,12A9,9 0 0,0 13,3")

HEAD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">\n  <title>{}</title>\n'
TILE_RECT = ('  <!-- Tile: dark rounded square, 12 percent corner radius -->\n'
             f'  <rect width="512" height="512" rx="61.44" fill="{TILE}"/>\n')


def concept_a(neutral, accent, title="sediment concept A"):
    """MDI layers-triple, newest layer in the accent, MDI history (a time arrow) as badge."""
    top, mid, low = MDI_LAYERS_TRIPLE_PARTS
    return (HEAD.format(title) + TILE_RECT +
            '  <!-- MDI layers-triple (Apache 2.0), 24 units scaled to 312 px; its three layers are the three sub-paths -->\n'
            '  <g transform="translate(70 62) scale(13)">\n'
            f'    <path fill="{neutral}" d="{low}"/>\n'
            f'    <path fill="{neutral}" d="{mid}"/>\n'
            f'    <path fill="{accent}" d="{top}"/>\n'
            '  </g>\n'
            '  <!-- Badge: MDI history (Apache 2.0) on a cut-out disc -->\n'
            f'  <circle cx="378" cy="378" r="96" fill="{TILE}"/>\n'
            f'  <path transform="translate(286 286) scale(7.7)" fill="{neutral}" d="{MDI_HISTORY}"/>\n'
            '</svg>\n')


def concept_b(neutral, accent, title="sediment concept B"):
    """A line chart whose samples thin out with age; the old spike survives in the accent."""
    sw = 18
    return (HEAD.format(title) + TILE_RECT +
            '  <!-- Time axis: L shaped axes, hand drawn -->\n'
            f'  <path d="M100 100V404H424" fill="none" stroke="{neutral}" stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/>\n'
            '  <!-- Old data: few samples, long straight segments. Recent data: many samples, fine zigzag -->\n'
            f'  <polyline points="136,338 214,302" fill="none" stroke="{neutral}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>\n'
            f'  <polyline points="294,302 316,330 334,280 352,322 370,272 388,314 406,262" fill="none" stroke="{neutral}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>\n'
            f'  <circle cx="136" cy="338" r="15" fill="{neutral}"/>\n'
            '  <!-- The kept spike, in the accent -->\n'
            f'  <polyline points="214,302 254,150 294,302" fill="none" stroke="{accent}" stroke-width="{sw + 2}" stroke-linecap="round" stroke-linejoin="round"/>\n'
            f'  <circle cx="254" cy="150" r="22" fill="{accent}"/>\n'
            '</svg>\n')


def concept_c(neutral, accent, title="sediment concept C"):
    """Strata of cells: 8, 4, 2, 1 cells per row, rows thicker with depth, one accent spike through all."""
    x0, width, gap_x, gap_y = 96, 320, 8, 12
    rows = [(16, 44), (8, 56), (4, 68), (1, 80)]  # (cells, height) from newest to oldest
    y = 132
    parts = []
    for n, h in rows:
        w = (width - (n - 1) * gap_x) / n
        r = 3 if n > 1 else 8
        for i in range(n):
            parts.append(f'    <rect x="{x0 + i * (w + gap_x):.2f}" y="{y}" width="{w:.2f}" height="{h}" rx="{r}"/>')
        y += h + gap_y
    bottom = y - gap_y
    sx = 208
    return (HEAD.format(title) + TILE_RECT +
            '  <!-- Strata, newest on top: the same time span in 16, 8, 4 and 1 cells; deeper rows are thicker -->\n'
            f'  <g fill="{neutral}">\n' + "\n".join(parts) + '\n  </g>\n'
            '  <!-- One spike kept through every stratum: tile colored slot, then the accent bar -->\n'
            f'  <rect x="{sx - 22}" y="132" width="44" height="{bottom - 132}" fill="{TILE}"/>\n'
            f'  <path d="M{sx - 12} {bottom}V104a12 12 0 0 1 24 0V{bottom}Z" fill="{accent}"/>\n'
            '</svg>\n')


def concept_b2(neutral, accent, title="sediment concept B2", badge=True):
    """Round 2: time runs left to right. Old data on the left is a few kept points joined by long
    straight segments (with the spike in the accent); recent data on the right is a dense zigzag."""
    tf = "translate(30 6) scale(0.86)" if badge else "translate(-6 4)"
    dense = [(306, 326), (319, 300), (332, 330), (345, 292), (358, 322), (371, 284),
             (384, 314), (397, 276), (410, 306)]
    pts = " ".join(f"{x},{y}" for x, y in dense)
    s = (HEAD.format(title) + TILE_RECT +
         f'  <g transform="{tf}">\n'
         '    <!-- Time axis, hand drawn: oldest at the left, newest at the right -->\n'
         f'    <path d="M100 100V404H424" fill="none" stroke="{neutral}" stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/>\n'
         '    <!-- Old data thinned out: long straight segments between a few kept points -->\n'
         f'    <polyline points="134,342 178,322 206,316" fill="none" stroke="{neutral}" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>\n'
         f'    <circle cx="134" cy="342" r="16" fill="{neutral}"/>\n'
         f'    <circle cx="178" cy="322" r="16" fill="{neutral}"/>\n'
         f'    <circle cx="306" cy="326" r="16" fill="{neutral}"/>\n'
         '    <!-- The kept spike, in the accent -->\n'
         f'    <polyline points="206,316 238,150 270,316" fill="none" stroke="{accent}" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/>\n'
         f'    <circle cx="238" cy="150" r="22" fill="{accent}"/>\n'
         f'    <polyline points="270,316 306,326" fill="none" stroke="{neutral}" stroke-width="18" stroke-linecap="round"/>\n'
         f'    <circle cx="206" cy="316" r="15" fill="{accent}"/>\n'
         f'    <circle cx="270" cy="316" r="15" fill="{accent}"/>\n'
         '    <!-- Recent data kept in full: dense, thinner jagged line -->\n'
         f'    <polyline points="{pts}" fill="none" stroke="{neutral}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>\n'
         '  </g>\n')
    if badge:
        s += ('  <!-- Badge: MDI history (Apache 2.0) on a cut-out disc -->\n'
              f'  <circle cx="394" cy="394" r="88" fill="{TILE}"/>\n'
              f'  <path transform="translate(307 307) scale(7.3)" fill="{neutral}" d="{MDI_HISTORY}"/>\n')
    return s + '</svg>\n'


def concept_a2(neutral, accent, title="sediment concept A2", badge=True):
    """Round 2: three sheets in an isometric stack, the plane drawn as a rhombus. The top sheet is an
    outline carrying a line graph skewed onto its plane; below it the sheets are cell grids that get
    coarser with depth (8 x 8 cells, then 4 x 4). The clock is a small badge, or left out."""
    dx, dy, step, cx, y1 = 132, 76, 78, 256, 98

    def at(oy, u, v):
        return (cx + (u - v) * dx, oy + (u + v) * dy)

    def poly(pts):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    def rhombus(oy):
        return poly([at(oy, 0, 0), at(oy, 1, 0), at(oy, 1, 1), at(oy, 0, 1)])

    def cells(oy, n, g=0.02):
        out = []
        for i in range(n):
            for j in range(n):
                u0, u1, v0, v1 = i / n + g, (i + 1) / n - g, j / n + g, (j + 1) / n - g
                out.append(f'    <polygon points="{poly([at(oy, u0, v0), at(oy, u1, v0), at(oy, u1, v1), at(oy, u0, v1)])}"/>')
        return "\n".join(out)

    y2, y3 = y1 + step, y1 + 2 * step
    mask = lambda oy: (f'  <polygon points="{rhombus(oy)}" fill="{TILE}" stroke="{TILE}" stroke-width="26" stroke-linejoin="round"/>\n')
    before = [(10, 70), (18, 56), (26, 72), (34, 54), (41, 70)]
    after = [(59, 70), (66, 52), (73, 72), (80, 52), (90, 70)]
    c = lambda pts: " ".join(f"{x},{y}" for x, y in pts)
    s = (HEAD.format(title) + TILE_RECT +
         '  <!-- Oldest sheet: coarse grid, 4 x 4 cells -->\n'
         f'  <g fill="{neutral}">\n{cells(y3, 4)}\n  </g>\n'
         '  <!-- Middle sheet: finer grid, 8 x 8 cells, over a tile colored gap -->\n' + mask(y2) +
         f'  <g fill="{neutral}">\n{cells(y2, 8)}\n  </g>\n'
         '  <!-- Newest sheet: outline plate with a line graph skewed onto its plane -->\n' + mask(y1) +
         f'  <polygon points="{rhombus(y1)}" fill="none" stroke="{neutral}" stroke-width="14" stroke-linejoin="round"/>\n'
         f'  <g transform="matrix({dx / 100} {dy / 100} {-dx / 100} {dy / 100} {cx} {y1})" fill="none" stroke-linecap="round" stroke-linejoin="round">\n'
         f'    <polyline points="{c(before)}" stroke="{neutral}" stroke-width="9"/>\n'
         f'    <polyline points="{c(after)}" stroke="{neutral}" stroke-width="9"/>\n'
         f'    <polyline points="41,70 50,14 59,70" stroke="{accent}" stroke-width="10"/>\n'
         f'    <circle cx="50" cy="14" r="6.5" fill="{accent}" stroke="none"/>\n'
         '  </g>\n')
    if badge:
        s += ('  <!-- Small badge: MDI history (Apache 2.0) on a cut-out disc -->\n'
              f'  <circle cx="412" cy="412" r="62" fill="{TILE}"/>\n'
              f'  <path transform="translate(357 354) scale(4.8)" fill="{neutral}" d="{MDI_HISTORY}"/>\n')
    return s + '</svg>\n'


CONCEPTS_R2 = {
    "concept-b2-badge": ("B2", "chart left to right + clock", lambda n, a, t: concept_b2(n, a, t, True)),
    "concept-b2-plain": ("B2", "chart left to right, no clock", lambda n, a, t: concept_b2(n, a, t, False)),
    "concept-a2-badge": ("A2", "pixelating sheets + small clock", lambda n, a, t: concept_a2(n, a, t, True)),
    "concept-a2-plain": ("A2", "pixelating sheets, no clock", lambda n, a, t: concept_a2(n, a, t, False)),
}


CONCEPTS = {
    "concept-a-layers": ("A", "layers + time arrow", concept_a),
    "concept-b-chart": ("B", "thinning line chart", concept_b),
    "concept-c-strata": ("C", "coarsening strata cells", concept_c),
}


def main():
    sib = {k: hex_to_oklch(v) for k, v in SIBLINGS.items()}
    neutral = oklch_to_hex(*NEUTRAL_LCH)
    L = sum(v[0] for v in sib.values()) / len(sib)
    C = sum(v[1] for v in sib.values()) / len(sib)

    def throw(start):
        n, out = start, []
        while True:
            seed = SEED if n == 1 else f"{SEED}#{n}"
            hue = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 360
            gaps = {k: round(hue_gap(hue, v[2]), 1) for k, v in sib.items()}
            ok = min(gaps.values()) >= MIN_HUE_GAP
            out.append({"seed": seed, "hue": hue, "gap_to_siblings": gaps, "accepted": ok})
            if ok:
                return out, n + 1
            n += 1

    darts, nxt = throw(1)
    hue = darts[-1]["hue"]
    extra = []
    for _ in range(EXTRA_DARTS):
        d, nxt = throw(nxt)
        extra += d
    accent, nudge = make_accent(L, C, hue, neutral)

    def free(h):
        return min(hue_gap(h % 360, v[2]) for v in sib.values()) >= MIN_HUE_GAP

    schemes = {"complementary": [hue + 180], "split-complementary": [hue + 150, hue + 210],
               "triadic": [hue + 120, hue + 240]}
    variants = {"dart": {"hue": hue, "dropped": False}}
    for name, hs in schemes.items():
        for h in hs:
            variants[f"{name} {h % 360}"] = {"hue": h % 360, "dropped": not free(h)}
    for d in extra:
        if d["accepted"]:
            variants[f"extra dart {d['seed']}"] = {"hue": d["hue"], "dropped": False}
    for k, v in variants.items():
        v["accent"], v["nudge"] = make_accent(L, C, v["hue"], neutral) if not v["dropped"] else (None, "dropped: within 25 degrees of a used accent")
        if v["accent"]:
            v.update(describe(v["accent"], neutral, sib))

    result = {
        "rule": __doc__.split("Rule, same as get-the-camera, step by step:")[1].split("Writes palette.json")[0].strip(),
        "siblings": {k: {"hex": SIBLINGS[k], "L": round(v[0], 3), "C": round(v[1], 3), "h": round(v[2], 1)} for k, v in sib.items()},
        "draft_hues_reported_only": {k: {"hue": v, "gap_to_chosen": round(hue_gap(v, hue), 1)} for k, v in DRAFT_HUES.items()},
        "darts": darts + extra, "target_L": round(L, 3), "target_C": round(C, 3),
        "neutral": {"hex": neutral, "oklch": list(NEUTRAL_LCH), "contrast_on_tile": round(contrast(neutral, TILE), 2)},
        "accent": {**describe(accent, neutral, sib), "nudge": nudge},
        "variants": variants,
    }
    (HERE / "palette.json").write_bytes((json.dumps(result, indent=2) + "\n").encode("utf-8"))
    for name, (_, _, fn) in {**CONCEPTS, **CONCEPTS_R2}.items():
        (HERE / f"{name}.svg").write_bytes(fn(neutral, accent, f"sediment {name}").encode("utf-8"))
    print(json.dumps({k: result[k] for k in ("darts", "neutral", "accent")}, indent=2))
    for k, v in variants.items():
        print(k, v["hue"], v["accent"], v.get("vs_neutral"), v["nudge"])


if __name__ == "__main__":
    main()
