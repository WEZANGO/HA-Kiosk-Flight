# sisl/aircraftshapes (vendored, MIT)

Source: <https://github.com/sisl/aircraftshapes> — *Aircraft shapes for use with the TikZ LaTeX
package*, by the Stanford Intelligent Systems Laboratory (SISL).

Licence: **MIT** (see `LICENSE.txt`, kept verbatim). MIT requires the copyright notice and the
permission notice to travel with copies of the software, which is why this directory exists rather
than just the converted output.

## What is here and what we did with it

* `aircraftshapes.sty` — the package as published. It declares eight PGF/TikZ shapes:
  `aircraft side`, `aircraft top`, `quadcopter side`, `quadcopter top`, `helicopter side`,
  `helicopter top`, `UAM side`, `UAM top`.
* We use **two** of them, the ones this app has no equivalent of its own: `aircraft side` (a
  generic airliner profile) and `helicopter side`. Both are plain polygon data
  (`\pgfpathmoveto` / `\pgfpathlineto`), so `tools/build_side_views.py` transcribes the points
  straight out of the `.sty` — no redrawing — flips the y axis for SVG and mirrors the nose to
  face left.

The rest of the side-view set (`regional`, `bizjet`, `turboprop`, `light`, `glider`, plus the
`heavy` and `quad` variants of the airliner profile) is authored in this repository, in the same
flat low-polygon style, because the library has one airliner and one helicopter and no way to say
"four engines" or "a Cessna".

**If you replace or extend these**, keep the two converted files recognisably derived from the MIT
geometry and leave this directory (with its LICENSE) in place.
