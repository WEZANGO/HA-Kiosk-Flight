# HA-Kiosk-Flight

A Home Assistant app (add-on) displaying the aircraft currently flying overhead, as a full-screen
kiosk radar. It reads the `flights` list that Home Assistant's Flightradar24 integration already
publishes on its *in area* sensor (via the Supervisor API) and draws everything locally — no map
tiles, no SDK, no external requests from the kiosk.

> **Status: v0.1.1 — installed and running on Home Assistant.**

## Start here

| File | What it is |
|---|---|
| `DOCS.md` | the human-facing documentation: install, settings, how it works, troubleshooting |
| `HOME_ASSISTANT_ADDON_GUIDE.md` | the handoff guide this app was built from: file skeleton, settings design, the three auth channels, local testing, shipping conventions |
| `app.py` | single-file stdlib-only server: display store, admin UI, display page, APIs |
| `web/display.html` | the kiosk page — local SVG radar, aircraft plots, tap-for-detail |

## Verified

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
* Any sensor in Home Assistant that publishes a `flights` list with positions can drive a display —
  the sensor picker is not hard-coded to Flightradar24.
