"""Build openField's logo files from one geometry.

    uv run --with uharfbuzz --with fonttools python scripts/build_brand.py

The mark: a sun rising on the horizon over rows of dots in perspective (an open field at dawn). One dot is
lit in the sun's colour: the job that is running. The wordmark is Outfit (SIL OFL), outlined to paths so
the files need no font installed.
"""
from __future__ import annotations

from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "src/openfield/web/fonts/Outfit-wght.ttf"
OUT = ROOT / "assets/brand"
WEB = ROOT / "src/openfield/web"

INK, PAPER = "#15181D", "#F6F4EE"
SUN_TOP, SUN_BOTTOM = "#FFB341", "#FF5E1A"
LIT = "#FF7A1A"


def sun_gradient(gid: str) -> str:
    return (f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{SUN_TOP}"/><stop offset="1" stop-color="{SUN_BOTTOM}"/></linearGradient>')


def field_dots(dot: str, far_opacity: bool) -> str:
    """Three rows converging on the sun at (32, 34); the second dot right of centre on the near row is lit."""
    rows = [(40, 1.5, 4.3, 0.55), (47, 2.1, 8.4, 0.78), (56, 2.8, 13.0, 1.0)]
    out = []
    for r_i, (y, r, step, op) in enumerate(rows):
        for k in range(-2, 3):
            x = 32 + k * step
            if r_i == 2 and k == 1:
                out.append(f'<circle cx="{x:g}" cy="{y}" r="{r + 0.4:g}" fill="{LIT}"/>')
                continue
            alpha = f' fill-opacity="{op:g}"' if far_opacity and op < 1 else ""
            out.append(f'<circle cx="{x:g}" cy="{y}" r="{r:g}" fill="{dot}"{alpha}/>')
    return "".join(out)


def mark_group(dot: str, gid: str, far_opacity: bool = True) -> str:
    sun = f'<path d="M20 34A12 12 0 0 1 44 34Z" fill="url(#{gid})"/>'
    horizon = f'<rect x="8" y="33.4" width="48" height="1.2" rx="0.6" fill="{dot}" fill-opacity="0.35"/>'
    return sun + horizon + field_dots(dot, far_opacity)


def svg(width: float, height: float, body: str, defs: str = "", label: str = "openField") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height:g}" width="{width:g}" '
            f'height="{height:g}" role="img" aria-label="{label}"><defs>{defs}</defs>{body}</svg>\n')


def wordmark_paths(size: float, x0: float, baseline: float, color: str) -> tuple[str, float]:
    """'open' at weight 400 and 'Field' at 600, kerned by HarfBuzz, drawn as paths. Returns (svg, width)."""
    data = FONT.read_bytes()
    face = hb.Face(hb.Blob(data))
    upem = face.upem
    scale = size / upem
    paths, x = [], 0.0
    for text, weight in (("open", 400), ("Field", 620)):
        font = hb.Font(face)
        font.set_variations({"wght": weight})
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(font, buf, {"kern": True, "liga": True})
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            pen = SVGPathPen(None)
            tx = x0 + (x + pos.x_offset) * scale
            ty = baseline - pos.y_offset * scale
            font.draw_glyph_with_pen(info.codepoint, TransformPen(pen, (scale, 0, 0, -scale, tx, ty)))
            d = pen.getCommands()
            if d:
                paths.append(d)
            x += pos.x_advance - 6  # a touch of negative tracking
    return f'<path fill="{color}" d="{" ".join(paths)}"/>', x * scale


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    g = sun_gradient("sun")
    # Marks (transparent) for light and dark backgrounds, and the app icon on a rounded ink square.
    (OUT / "openfield-mark.svg").write_text(svg(64, 64, mark_group(INK, "sun"), g))
    (OUT / "openfield-mark-dark.svg").write_text(svg(64, 64, mark_group(PAPER, "sun"), g))
    icon = f'<rect width="64" height="64" rx="14" fill="{INK}"/>' + mark_group(PAPER, "sun")
    (OUT / "openfield-icon.svg").write_text(svg(64, 64, icon, g))
    # Favicon: fewer, bigger dots so it reads at 16 px.
    fav = (f'<rect width="32" height="32" rx="7" fill="{INK}"/><path d="M8 17A8 8 0 0 1 24 17Z" fill="url(#sun)"/>'
           f'<circle cx="10.5" cy="21.5" r="1.5" fill="{PAPER}" fill-opacity=".7"/><circle cx="16" cy="21.5" r="1.5" fill="{PAPER}" fill-opacity=".7"/>'
           f'<circle cx="21.5" cy="21.5" r="1.5" fill="{PAPER}" fill-opacity=".7"/><circle cx="5.5" cy="27" r="2.1" fill="{PAPER}"/>'
           f'<circle cx="16" cy="27" r="2.1" fill="{PAPER}"/><circle cx="26.5" cy="27" r="2.4" fill="{LIT}"/>')
    (WEB / "favicon.svg").write_text(svg(32, 32, fav, g))
    (OUT / "openfield-favicon.svg").write_text(svg(32, 32, fav, g))
    # Horizontal logos: mark + wordmark, baseline on the near field row.
    # The artwork spans y 22..59 of the 64-unit grid, so the horizontal logos crop to y 19..62.
    top, height = 19, 43
    for name, color in (("openfield-logo.svg", INK), ("openfield-logo-dark.svg", PAPER)):
        words, w = wordmark_paths(35, 76, 48.5, color)
        doc = svg(76 + w + 6, height, mark_group(color, "sun") + words, g)
        (OUT / name).write_text(doc.replace(f'viewBox="0 0 {76 + w + 6:g} {height}"', f'viewBox="0 {top} {76 + w + 6:g} {height}"'))
    # Inline versions for the web UI (colour follows the theme through currentColor).
    words, w = wordmark_paths(35, 76, 48.5, "currentColor")
    inline = (f'<svg class="logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 {top} {76 + w + 6:g} {height}" role="img" '
              f'aria-label="openField"><defs>{sun_gradient("sun-ui")}</defs>{mark_group("currentColor", "sun-ui")}{words}</svg>')
    (WEB / "logo-inline.svg").write_text(inline + "\n")
    print("wrote", sorted(p.name for p in OUT.iterdir()), "+ web favicon.svg, logo-inline.svg")


if __name__ == "__main__":
    main()
