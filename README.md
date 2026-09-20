# HA-Kiosk-Flight

A Home Assistant app (add-on) displaying the aircraft currently flying overhead, as a full-screen
kiosk radar. It reads the `flights` list that Home Assistant's Flightradar24 integration already
publishes on its *in area* sensor (via the Supervisor API) and draws everything locally — no map
tiles, no SDK, no external requests from the kiosk.

It has two full-screen views, from one display and one settings set:

* the **radar** — every aircraft in the sensor's area, plotted and labelled;
* the **single-aircraft dashboard** — the whole screen for one aircraft: its **airline logo**,
  flight number, a top-down **schematic of the aircraft type**, altitude and
  origin → destination. It can *be* the display (standalone), or the radar can switch to it
  automatically when only one aircraft is left, or when an aircraft is tapped.

> **Status: v0.2.1 — the dashboard and the airline logos were added and verified against live
> traffic; the v0.1.x radar behaviour is unchanged.**

## Start here

| File | What it is |
|---|---|
| `DOCS.md` | the human-facing documentation: install, settings, how it works, troubleshooting |
| `HOME_ASSISTANT_ADDON_GUIDE.md` | the handoff guide this app was built from: file skeleton, settings design, the three auth channels, local testing, shipping conventions |
| `app.py` | single-file stdlib-only server: display store, admin UI, display page, APIs, the aircraft-type → icon table |
| `web/display.html` | the kiosk page — local SVG radar **and** the single-aircraft dashboard, tap-for-detail |
| `web/aircraft_icons.json` | generated: the aircraft silhouettes, inlined into the page (never fetched) |
| `vendor/adsb-radar/` | the vendored icon artwork, its licence and the attribution it requires |
| `tools/build_aircraft_icons.py` | re-normalises the artwork into `web/aircraft_icons.json` |
| `/data/logos/<ICAO>.png` | *(runtime, not in the repo)* airline logos fetched once by the app — it is the only thing this app ever fetches from the internet |

## Aircraft icons

The aircraft schematics are **not** mine and are not free-floating: they come from
[ADS-B Radar for macOS](https://adsb-radar.com) (37 top-down aircraft silhouettes, free for
personal and commercial use in exchange for a backlink), the same terms are repeated in
`vendor/adsb-radar/README.md`, `DOCS.md`, the app's admin page and the credit line of both
views. Nothing else in this repo is third-party artwork — the airline logos are fetched at
runtime, by the app, from Flightradar24's own operator set, and are the airlines' marks (see
"Airline logos" in `DOCS.md`).

## Verified

*(Everything below is measured against the running app; v0.2.x additions are marked.)*

* **The airline logo, in real pixels, including the reported case** (v0.2.1): a fixture pinned to
  a live Aer Lingus aircraft shows the Aer Lingus wordmark **loaded** as an `<img>` on a light
  plate (`naturalWidth 140`, on-screen box 140×27 inside a 164×97 badge, plate
  `rgba(238,245,255,0.94)`), which is the check that matters — "the payload has a logo" is not the
  same as "the logo is on the screen". A dark navy wordmark (Air France) is verified the same way:
  that is the case the plate exists for. Then the two fallbacks: an airline whose ICAO code has **no
  logo file** (HiSky/HYM) shows the coloured code badge, and the *Monogram badge* setting sends
  **zero logo bytes** and shows the code — no broken images in any state.
* **The logo pipeline's rules were each asserted** (v0.2.1): one fetch per airline cached to
  `/data/logos/<ICAO>.png`; keys are ICAO codes (`EIN`) not IATA (`EI`, which 404s); a second poll
  is byte-identical and re-fetches nothing; every payload key belongs to an airline actually in that
  payload; and a 404 is not treated as "the internet is down".

* **The single-aircraft dashboard, driven in a real browser against this live Home Assistant**
  (1920×1080, 1080×1920 and the admin modal; 23 assertions, all passing): the standalone
  dashboard renders with the radar hidden, every field populated from real traffic
  (`EI725 / Airbus A320-251N / 777 m / LHR London → ORK Cork`, badge `EI`), the schematic is a
  real SVG drawing sized inside the viewport, the credit line carries the required attribution,
  the size slider scales every size by exactly 2.00×, and nothing throws in the page.
* **The three ways in were each exercised**, not just wired up: auto mode stays on the radar with
  281 aircraft in the area and switches to the dashboard when the same live payload is one
  aircraft (fixture = the live payload truncated, marked as such); a **real mouse click** on a
  label opens the dashboard for *that* aircraft (asserted against the payload's own flight number
  for the tapped callsign); a click on the dashboard returns to the radar; and a display pointed at
  a non-existent sensor shows the reason **on the dashboard** instead of a blank screen.
* **The layout collision that a screenshot caught**: in landscape the centred block reached the
  credit line and printed through it (content ended at 952 px, credit started at 995 px after the
  fix, in portrait 1372/1835) — now asserted as a rectangle test at both aspect ratios.
* **A silent JS error was found and fixed by this exercise**: the dashboard's DOM helper was named
  `detailRows`, which the radar already used for its bottom strip — the later declaration wins at
  *every* call site, so the dashboard called the strip's version with no aircraft and threw on
  `flight.registration`. The whole render died and the error was reported on the radar the
  dashboard was covering; `showNotice()` now brings the radar forward for an error, so a render
  failure can never be invisible again.
* **The admin round trip, through the real modal**: add a display → tick *standalone* and
  *auto*, set the dashboard slider to 150% → Save → stored as `detailAlways=true`,
  `detailAuto=true`, `detailClick=false`, `sizeDetail=150` → reopen shows switches and readout
  restored → the saved display renders as a dashboard with `--ds: 1.5` applied.
* **The icon pipeline**: all 37 vendored SVGs are re-normalised to `currentColor` and rendered as
  a contact sheet (they first came out **half black** — most icons declare no `fill` at all, so
  without `fill="currentColor"` on the root they inherit SVG's default black; caught by looking at
  the sheet, not by asserting the JSON was non-empty).
* **Type matching against 281 live aircraft**: `BE20` → light twin, `A189` → helicopter,
  `A320`/`A20N`/`A21N` → A320, `B38M`/`B738` → 737, `AT76` → Dash 8/ATR — checked by printing the
  resolved icon per live row, not by reading the table.
* The v0.1.x radar behaviour was re-checked in the same run (radar visible with the dashboard
  hidden, 12 plots labelled, no page errors) — adding a second view did not disturb the first.
* **Earlier, v0.1.x evidence** (unchanged by this work) follows.

### Earlier evidence (v0.1.x)

* **Run against this live Home Assistant** (v0.1.1): the display renders the actual aircraft
  in the sensor's area — 4 at once, from a 25 ft local (departing A320) to a BA 777 at
  36,000 ft / 47 km — with the sensor's own 100 km × 100 km box drawn and labelled on the
  scope, and the footer reporting `area ≈50 km · 4 airborne of 4`.
* **A real bug was found by using real data**: the Flightradar24 integration publishes
  `on_ground` as an **integer 0/1**, and reading it with a string-only boolean test made `0`
  look truthy — so "hide aircraft on the ground" (the default) hid *every* aircraft and the
  display looked empty no matter how far the sensor looked. Fixed in `truthy()`, and the
  fixture now emits `on_ground` as 0/1 as well so a bool-typed fixture cannot mask it again.
* The sweep rotates about the centre of the scope: measured through a rotation
  (177°→333° at ~51°/s, i.e. 7 s per revolution) the leading edge stays 232 px from the
  scope centre against an invariant of 233 px — 1 px deviation.
* Label placement, checked numerically in the live page (rect intersections): 0 label
  overlaps, 0 clipped labels, 0 labels over the compass or the area label, across metric,
  aviation and imperial displays and a 12-aircraft stress fixture.
* The accent colour was a setting nothing read (choosing a colour changed nothing). It now
  drives the glyphs, trails, rings, compass, sweep and home glow — verified by rendering the
  same display in two colours and diffing the computed styles, e.g. trail stroke
  `rgb(125,211,252)` → `rgb(255,176,32)`.
* Each text-size slider was checked by doubling it and measuring the rendered font size
  (count 129 px → 258 px, callsign 24.8 px → 49.7 px, compass 21 → 41, all exactly 2×), and
  the whole round trip through the admin modal (slide → save → reopen → values and readouts
  restored → display renders with them).
* The home-marker ripple twitched at its **outer full width** just before restarting. Cause:
  the expansion animated `width/height/margin` — layout properties — so at the loop boundary
  the fresh (tiny) geometry could paint a frame late while the ring was visible again, showing
  the previous 190 px ring brightly; the old keyframes also started at `opacity: .9` on top of a
  base state of `opacity: 1`. Stepping the animation's clock across the boundary with a
  one-frame geometry lag modelled: the old keyframes produce **8 such frames (96 px radius at
  opacity 1)**, the current ones **0** (worst case 96 px at opacity 0.03 — invisible). The
  ripple now expands via a composited `transform`, uses `animation-fill-mode: both` with an
  invisible base state, and holds `opacity: 0` for the first 10% of each cycle so any lagging
  frame lands in an invisible window. Frame-level captures (real compositor frames across the
  boundary) show no transient: max inter-frame luminance change 1.3 against a background of 11.
* Screenshots at 1920×1080, 1080×1920 and 3840×2160, plus the admin modal, the
  unsaved-settings preview and the aircraft detail tap.

Reference implementations to read alongside this one:

| Repo | Port | Value |
|---|---|---|
| `../HA-Kiosk-Navigation` | 8099 | per-display settings, options-held API key, full proxy of a third-party SDK + tiles + JSON APIs |
| `../HA-Kiosk-News` | 8098 | multi-source settings, server-side content fetch, image proxying with a user-facing toggle |
| `../HA-Kiosk-Canvas` | 8097 | reading Home Assistant entity values via the Supervisor API (`homeassistant_api: true`) |

## Notes for whoever builds on this

* The **data source is a Home Assistant sensor**, not a third-party API called by the app: the
  Flightradar24 integration owns the upstream session and this app only reads entity state. That is
  what makes isolated-VLAN kiosks work with no proxy at all.
* The display works in **polar coordinates computed server-side** (`bearing`, `distanceKm`), so the
  client never does trigonometry and never needs to know where north is on screen.
* Label placement is scored, not greedy: `placementCost()` prefers free space, treats the compass
  letters and range labels as expensive obstacles, and penalises running off the scope. The range
  labels themselves are placed in the quadrant furthest from every aircraft.
* **A third-party asset set is vendored, never fetched.** The aircraft schematics are files in
  `vendor/` re-normalised by `tools/build_aircraft_icons.py` into one JSON map that the app inlines
  into the page. Inlining is the load-bearing decision, not a nicety: the admin's unsaved-settings
  preview renders the page from `srcdoc`, where there is no base URL to fetch an `<img src>` from,
  and the kiosk VLAN has no internet at all. Check the licence's attribution requirement *before*
  wiring the artwork in — here it is a backlink, and it lives in four places (see DOCS.md).
* **`display.html` is one flat IIFE**: a name declared twice silently overrides at *every* call
  site (function declarations hoist). A new `detailRows()` for the dashboard collided with the
  radar's own `detailRows(flight)`, so the dashboard called the strip's version and threw on the
  first undefined field — a total render failure reported by an error notice the dashboard was
  covering. Before adding a helper, grep the page for the name; check the console (`window.__errors`)
  in any UI verification run, and never let an error state live on a view the current mode hides.
* **A view that a mode hides is a view whose errors are hidden too.** `showNotice(…, isError=true)`
  now switches back to the radar first, so a client-side render failure is always readable.
* **The middle path between "proxy it" and "vendor it": fetch once, cache on disk, inline the
  bytes.** Airline logos are a closed-looking set whose *membership* is only known at runtime, so
  neither a static vendor nor a per-request proxy fits. The app fetches in a **background thread**
  (a poll answers with what is cached — a dead CDN costs a fallback badge for a few seconds, never a
  stalled display), keeps the artwork in `/data/logos/<ICAO>.png` forever, hands the page a `data:`
  URI (so it renders in the `srcdoc` preview too), remembers failures (one miss = an hour off; three
  network failures = the fetcher stands down for 30 minutes and says so in the log), and treats 404
  as "this airline has no file", not "the internet is down". It also only ever asks for airlines the
  display is actually showing.
* Any sensor in Home Assistant that publishes a `flights` list with positions can drive a display —
  the sensor picker is not hard-coded to Flightradar24.
