# Side-view aircraft profiles

Nine profiles, drawn side-on, for the dashboard's **Aircraft type graphic → Side view** setting.
They are *generated*: `python3 tools/build_side_views.py` writes these files, and
`tools/build_aircraft_icons.py` then folds them into `web/aircraft_icons.json` under `side-…` keys
(alongside the top-down set, which keeps its own names). **Re-run both after editing either.**

| File | What it is | Where it comes from |
|---|---|---|
| `jet.svg` | generic airliner, twin-jet | converted from `aircraft side` in **sisl/aircraftshapes** (MIT — see `../aircraftshapes/`) |
| `heli.svg` | light helicopter, skids and rotor | converted from `helicopter side` in the same library |
| `heavy.svg` | widebody twin | that same airliner polygon, scaled longer and deeper, plus two nacelles |
| `quad.svg` | four-engine jet | the same, stretched further, plus four nacelles |
| `regional.svg` | regional jet: tubby, rear engines, T-tail | authored here |
| `bizjet.svg` | business jet: sleek, rear engines, T-tail | authored here |
| `turboprop.svg` | twin turboprop: high wing, two propeller discs | authored here |
| `light.svg` | light single: high wing, nose propeller, fixed gear | authored here |
| `glider.svg` | glider: slender, long thin wing, T-tail, no engine | authored here |

## Fidelity, stated plainly

A profile distinguishes aircraft by **class**, not by family — engine count, wing position,
propellers, tail — so there are nine profiles where the top-down set has thirty-seven. An A321 and
a 737 share `jet`; a 777 and a 787 share `heavy`; a 747, an A340 and an A380 share `quad`. The
mapping from the already-resolved plan-view icon is `SIDE_BY_ICON` in `app.py`, and a type with no
profile at all (a drone, a balloon, a hang glider) keeps its plan view rather than being given a
wrong one.

The authored profiles are simple flat polygons in the same style as the converted ones, not
technical drawings: they are meant to be recognisable at a glance on a kiosk, from across a room.

## Conventions

* Nose points **left**, tail right — like the aircraft profile art people are used to.
* Every profile is held in a **y-up** box (nose near x = −1) and flipped to SVG's y-down space once,
  in `svg_for()`. Mixing conventions per file is how the first attempt produced upside-down
  aeroplanes with engine pods floating in the sky.
* Output is `viewBox` + `fill="currentColor"` paths only: the page's CSS sets the colour and the
  size, exactly as it does for the top-down icons.
