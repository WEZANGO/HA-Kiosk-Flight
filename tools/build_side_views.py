#!/usr/bin/env python3
"""Build the side-view aircraft profiles in vendor/side-views/.

    python3 tools/build_side_views.py            # write the SVGs
    python3 tools/build_side_views.py --sheet    # ...and a contact sheet in /tmp

Provenance is mixed on purpose and is recorded per file:

* ``jet`` and ``heli`` are converted from the **MIT-licensed** TikZ shape library
  `sisl/aircraftshapes` (vendor/aircraftshapes/, notice in LICENSE.txt). The
  conversion is a straight transcription of its polygon geometry — no redrawing —
  with the y axis flipped for SVG and the nose mirrored to face left.
* ``heavy`` and ``quad`` are that same MIT polygon, scaled non-uniformly (and, for
  the quad, with two extra nacelles) to read as a widebody and a four-engine jet.
* ``regional``, ``bizjet``, ``turboprop``, ``light`` and ``glider`` are authored
  here, in the same flat low-polygon style so the set looks like one family
  (see the note in vendor/side-views/README.md about what that means).

Every profile is drawn in a normalised box: nose near x = -1, tail near x = +1,
centreline y = 0, and y up. The output keeps only `viewBox` + one or more paths
with `fill="currentColor"`, so the page's CSS colours and sizes it exactly like
the top-down icons.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "vendor" / "aircraftshapes" / "aircraftshapes.sty"
TARGET = ROOT / "vendor" / "side-views"


# --------------------------------------------------------------------------- #
# The MIT shapes: read the TikZ polygons straight out of the .sty
# --------------------------------------------------------------------------- #
def tikz_shape(name: str):
    text = SOURCE.read_text()
    start = text.index(f"\\pgfdeclareshape{{{name}}}{{")
    end = text.index("\\pgfdeclareshape{", start + 10)
    body = text[start:end].replace("\r", "")
    subpaths, current = [], []
    for command, x, y in re.findall(
            r"\\(pgfpath\w+)\{\\pgfpoint\{([^}]*)\}\{([^}]*)\}\}", body):
        point = (float(x), float(y))            # kept in PGF's y-UP convention
        if command == "pgfpathmoveto":
            if current:
                subpaths.append(current)
            current = [point]
        elif command == "pgfpathlineto":
            current.append(point)
    if current:
        subpaths.append(current)
    return [sub for sub in subpaths if len(sub) > 2]


# --------------------------------------------------------------------------- #
# Transforms and authored profiles
# --------------------------------------------------------------------------- #
def mirror_x(subpaths):
    return [[(-x, y) for x, y in sub] for sub in subpaths]


def scale(subpaths, sx=1.0, sy=1.0, dy=0.0):
    return [[(x * sx, y * sy + dy) for x, y in sub] for sub in subpaths]


def nacelles(count, x0, y0, length, height, gap):
    """Engine pods (rectangles) in a row under a wing — the only 'extra' the
    derived widebody/quad profiles get, so nothing is invented about the shape."""
    pods = []
    for index in range(count):
        x = x0 + index * gap
        pods.append([(x, y0), (x, y0 + height), (x + length, y0 + height),
                     (x + length, y0), (x, y0)])
    return pods


# Authored profiles. Straight polygons only, matching the flat style of the
# converted ones: a near wing sweeping back and down, a fin (T-tail where the real
# aircraft has one), engine pods or propeller discs, and gear where it shows.
AUTHORED = {
    # Regional jet: short and tubby, rear-fuselage engines, T-tail.
    "regional": [
        [(-1.0, 0.0), (-0.93, 0.055), (-0.55, 0.075), (0.30, 0.075), (0.66, 0.06),
         (0.72, 0.30), (0.80, 0.30), (0.90, 0.09), (0.97, 0.05), (0.99, -0.005),
         (0.90, -0.035), (0.35, -0.055), (-0.30, -0.06), (-0.94, -0.035)],
        [(0.70, 0.28), (0.70, 0.335), (1.0, 0.30), (1.0, 0.26), (0.70, 0.28)],
        [(-0.20, -0.005), (0.36, -0.02), (0.52, -0.26), (0.28, -0.30)],
        # Rear-fuselage engine: a pod that hangs INTO the belly line, not a bump on it.
        [(0.30, -0.10), (0.30, 0.02), (0.68, 0.02), (0.68, -0.10)],
    ],
    # Business jet: sleek, rear engines, T-tail, small wing.
    "bizjet": [
        [(-1.0, 0.0), (-0.95, 0.04), (-0.55, 0.062), (0.30, 0.068), (0.68, 0.05),
         (0.76, 0.26), (0.84, 0.26), (0.92, 0.07), (0.98, 0.035), (0.99, -0.005),
         (0.90, -0.03), (0.30, -0.048), (-0.35, -0.05), (-0.95, -0.03)],
        [(0.74, 0.245), (0.74, 0.295), (1.0, 0.265), (1.0, 0.225), (0.74, 0.245)],
        [(-0.18, 0.0), (0.30, -0.015), (0.44, -0.22), (0.24, -0.26)],
        [(0.26, -0.085), (0.26, 0.015), (0.58, 0.015), (0.58, -0.085)],
    ],
    # Twin turboprop: high straight wing, two propeller discs, fat fuselage.
    "turboprop": [
        [(-1.0, 0.01), (-0.92, 0.075), (-0.45, 0.10), (0.40, 0.095), (0.80, 0.07),
         (0.86, 0.28), (0.95, 0.28), (1.0, 0.06), (1.0, -0.005),
         (0.90, -0.045), (0.40, -0.075), (-0.30, -0.08), (-0.93, -0.05)],
        # Wing on TOP of the fuselage (it is a high-wing aircraft), not through it.
        [(-0.30, 0.095), (0.44, 0.09), (0.44, 0.045), (-0.30, 0.055)],
        # The near nacelle, tucked just under that wing...
        [(-0.06, 0.0), (-0.06, 0.065), (0.32, 0.065), (0.32, 0.0)],
        # ...with its propeller disc crossing the wing's leading edge (a bar that
        # floats above the fuselage reads as a mast, which is what the first
        # attempt looked like).
        [(-0.13, 0.17), (-0.09, 0.17), (-0.09, -0.09), (-0.13, -0.09)],
        [(0.53, 0.15), (0.57, 0.15), (0.57, -0.05), (0.53, -0.05)],
        [(-0.60, -0.08), (-0.60, -0.15), (-0.48, -0.15), (-0.48, -0.08)],
        [(0.16, -0.08), (0.16, -0.14), (0.27, -0.14), (0.27, -0.08)],
    ],
    # Light single: high wing, one propeller, fixed gear, strut.
    "light": [
        [(-1.0, 0.0), (-0.92, 0.055), (-0.45, 0.075), (0.45, 0.07), (0.78, 0.05),
         (0.84, 0.21), (0.93, 0.21), (0.97, 0.045), (0.99, -0.01),
         (0.90, -0.04), (0.30, -0.055), (-0.45, -0.06), (-0.93, -0.04)],
        # High wing sitting on the cabin roof.
        [(-0.08, 0.075), (0.54, 0.07), (0.54, 0.032), (-0.08, 0.04)],
        # Cabin strut, wing down to the lower fuselage.
        [(0.02, 0.03), (0.30, 0.045), (0.32, 0.02), (0.04, -0.04)],
        # Propeller disc right at the nose.
        [(-0.99, 0.15), (-0.955, 0.15), (-0.955, -0.14), (-0.99, -0.14)],
        # Fixed gear: two legs and two wheels.
        [(-0.52, -0.06), (-0.50, -0.06), (-0.50, -0.13), (-0.52, -0.13)],
        [(0.12, -0.06), (0.14, -0.06), (0.14, -0.12), (0.12, -0.12)],
        [(-0.56, -0.17), (-0.46, -0.17), (-0.46, -0.13), (-0.56, -0.13)],
        [(0.08, -0.16), (0.18, -0.16), (0.18, -0.12), (0.08, -0.12)],
    ],
    # Glider: slender fuselage, long thin swept wing, T-tail, no engine.
    "glider": [
        [(-1.0, 0.0), (-0.96, 0.035), (-0.50, 0.05), (0.45, 0.04), (0.86, 0.03),
         (0.72, 0.03), (0.78, 0.20), (0.87, 0.20), (0.94, 0.03), (0.99, 0.0),
         (0.90, -0.03), (0.30, -0.04), (-0.40, -0.04), (-0.96, -0.03)],
        [(0.76, 0.19), (0.76, 0.24), (1.0, 0.21), (1.0, 0.17), (0.76, 0.19)],
        # Long, thin, gently swept wing — a glider's wing, not a blade.
        [(-0.30, 0.03), (0.20, 0.015), (0.60, -0.11), (0.52, -0.145)],
    ],
}


def svg_for(subpaths, name: str) -> str:
    """Emit SVG. Every profile — converted or authored — is held in a y-UP
    convention (nose near x=-1), which is what the source TikZ geometry uses; the
    flip to SVG's y-down space happens HERE, once, so an authored profile and a
    converted one cannot end up mirrored or upside down relative to each other
    (they did: the hand-drawn ones came out inverted and the engine pods floated
    above the aeroplane)."""
    xs = [x for sub in subpaths for x, _ in sub]
    ys = [y for sub in subpaths for _, y in sub]
    pad = 0.01
    min_x, max_x, min_y, max_y = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    data = []
    for sub in subpaths:
        points = [(x - min_x, max_y - y) for x, y in sub]
        data.append("M" + " L".join(f"{x:.4f} {y:.4f}" for x, y in points) + " Z")
    width, height = max_x - min_x, max_y - min_y
    return (f'<svg viewBox="0 0 {width:.4f} {height:.4f}" preserveAspectRatio="xMidYMid meet" '
            f'fill="currentColor" focusable="false" aria-hidden="true">'
            f'<path d="{" ".join(data)}"/></svg>')


def main() -> None:
    jet = mirror_x(tikz_shape("aircraft side"))
    heli = mirror_x(tikz_shape("helicopter side"))
    profiles = {
        # The converted MIT shapes, and two derived from the airliner one: a
        # stretched/deepened fuselage reads as a widebody, and the quad adds a
        # second pair of nacelles to the same outline.
        "jet": jet,
        "heli": heli,
        # A widebody twin: the same airliner outline, longer and deeper, with two
        # nacelles tucked against the belly. The quad gets four short ones, which
        # is how a flat side drawing says "four engines" at a glance.
        "heavy": scale(jet, sx=1.10, sy=1.28) + nacelles(2, -0.30, -0.105, 0.32, 0.08, 0.36),
        "quad": scale(jet, sx=1.16, sy=1.36) + nacelles(4, -0.38, -0.115, 0.18, 0.075, 0.25),
    }
    profiles.update(AUTHORED)

    TARGET.mkdir(parents=True, exist_ok=True)
    for name, subpaths in profiles.items():
        svg = svg_for(subpaths, name)
        (TARGET / f"{name}.svg").write_text(svg + "\n")
        print(f"{name:10} {len(subpaths)} sub-paths  {len(svg) / 1024:.1f} KB")

    if "--sheet" in sys.argv:
        cells = []
        for name in profiles:
            body = (TARGET / f"{name}.svg").read_text()
            cells.append(f'<div class="cell"><div class="ic">{body}</div><b>{name}</b></div>')
        sheet = """<!doctype html><meta charset="utf-8"><style>
body{background:#070c16;margin:0;padding:16px;font:14px system-ui;color:#8fb6d4}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
.cell{background:#0b1220;border:1px solid #26364f;border-radius:10px;padding:10px;text-align:center}
.ic{color:#9fd8ff;display:block}
.ic svg{width:100%;max-width:900px;height:auto;display:block;margin:0 auto}
b{color:#ffd7a1}
</style><div class="grid">""" + "".join(cells) + "</div>"
        Path("/tmp/sideviews/sheet-live.html").write_text(sheet)
        print("sheet: /tmp/sideviews/sheet-live.html")
    print(f"{len(profiles)} profiles -> {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()