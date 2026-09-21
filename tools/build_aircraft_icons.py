#!/usr/bin/env python3
"""Build web/aircraft_icons.json from the vendored aircraft artwork.

    python3 tools/build_aircraft_icons.py

Two sets go in, under one map, because the display page only ever needs one of
them for a given display:

  * the top-down silhouettes from ADS-B Radar (vendor/adsb-radar/, see its README)
    — keys are their file stems: ``a320``, ``b737``, ``cessna`` …
  * the side-view profiles (vendor/side-views/, built by tools/build_side_views.py)
    — keys are prefixed ``side-``: ``side-jet``, ``side-heli`` …

Why a build step at all: the artwork ships as standalone SVG *files*, and this app
never lets the kiosk fetch an image — the page arrives with its icons embedded
(a page rendered from `srcdoc` in the admin preview has no base URL to fetch
from, and a kiosk on a VLAN with no internet has nowhere to fetch from anyway).
So the artwork is re-normalised here into one JSON map that app.py inlines.

Normalisation (the drawings themselves are NOT modified):
  * drop the XML declaration, the `width`/`height`/`version` attributes and the
    `xmlns:bx` namespace — the root keeps its `viewBox` and `preserveAspectRatio`
    so the CSS controls the size;
  * every fill and stroke colour becomes `currentColor`, and the root carries
    `fill="currentColor"` — most icons declare no fill at all, so without it they
    inherit SVG's default black and CSS `color` has nothing to reach (that is
    exactly how half the set first came out black on a dark background);
  * the BoxySVG editor's `<defs><bx:grid …/></defs>` guide is dropped (it is a
    drawing aid, not artwork, and its namespace prefix was removed with the
    other root attributes);
  * whitespace between tags is collapsed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "vendor" / "adsb-radar"
SIDE_SOURCE = ROOT / "vendor" / "side-views"
TARGET = ROOT / "web" / "aircraft_icons.json"

COLOUR = r"(?:#[0-9a-fA-F]{3,8}|rgb\([^)]*\)|[a-zA-Z]+)"


def normalise(svg_text: str) -> str:
    root = re.search(r"<svg\b[^>]*>", svg_text)
    if not root:
        raise ValueError("no <svg> root")
    view_box = re.search(r'viewBox="([^"]+)"', root.group(0))
    if not view_box:
        # Every icon in this set carries one; a default keeps the fallback sane.
        view_box_text = "0 0 200 200"
    else:
        view_box_text = view_box.group(1)
    body = svg_text[root.end():svg_text.rindex("</svg>")]
    body = re.sub(r"<defs>.*?</defs>", "", body, flags=re.S)
    # Colour -> currentColor, in both presentation attributes and inline styles.
    body = re.sub(r'fill="' + COLOUR + r'"', 'fill="currentColor"', body)
    body = re.sub(r'stroke="' + COLOUR + r'"', 'stroke="currentColor"', body)
    body = re.sub(r"(fill|stroke)\s*:\s*" + COLOUR, r"\1: currentColor", body)
    body = re.sub(r"\s+", " ", body).strip()
    body = re.sub(r">\s+<", "><", body)
    return (f'<svg viewBox="{view_box_text}" preserveAspectRatio="xMidYMid meet" '
            f'fill="currentColor" focusable="false" aria-hidden="true">{body}</svg>')


def main() -> None:
    icons = {}
    for path in sorted(SOURCE.glob("*.svg")):
        icons[path.stem] = normalise(path.read_text())
    if not icons:
        raise SystemExit(f"no icons found in {SOURCE}")
    # Side views are already normalised by tools/build_side_views.py; they are only
    # prefixed here so the two sets cannot collide in one map.
    side = 0
    for path in sorted(SIDE_SOURCE.glob("*.svg")):
        icons[f"side-{path.stem}"] = normalise(path.read_text())
        side += 1
    payload = json.dumps(icons, separators=(",", ":"), sort_keys=True)
    TARGET.write_text(payload + "\n")
    print(f"{len(icons)} icons ({side} of them side views) -> "
          f"{TARGET.relative_to(ROOT)} ({len(payload) / 1024:.1f} KB)")
    print("names:", " ".join(sorted(icons)))


if __name__ == "__main__":
    main()