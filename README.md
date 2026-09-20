# HA-Kiosk-Flight

A Home Assistant app (add-on) displaying the aircraft currently flying overhead, as a full-screen
kiosk radar. It reads the `flights` list that Home Assistant's Flightradar24 integration already
publishes on its *in area* sensor (via the Supervisor API) and draws everything locally — no map
tiles, no SDK, no external requests from the kiosk.

> **Status: first release (0.1.0), not yet installed on Home Assistant.**
> It has been run and verified locally against this Home Assistant instance (see *Verified* below).

## Start here

| File | What it is |
|---|---|
| `DOCS.md` | the human-facing documentation: install, settings, how it works, troubleshooting |
| `HOME_ASSISTANT_ADDON_GUIDE.md` | the handoff guide this app was built from: file skeleton, settings design, the three auth channels, local testing, shipping conventions |
| `app.py` | single-file stdlib-only server: display store, admin UI, display page, APIs |
| `web/display.html` | the kiosk page — local SVG radar, aircraft plots, tap-for-detail |

## Verified

* Live Home Assistant read through the same code path the Supervisor API uses (the LAN REST API
  stands in for `http://supervisor/core/api`): `sensor.flightradar24_current_in_area` is found,
  `zone.home` supplies the centre, and an empty area renders as "No aircraft overhead".
* Aircraft positions, bearings, distances, trails, units and label placement were checked against
  **real Flightradar24 data** (4 and then 12 live aircraft out of FR24's public feed, reshaped into
  the integration's exact attribute structure): 0 label overlaps, 0 clipped labels, 0 labels over
  the compass or range text at the default 6 aircraft shown.
* Screenshots at 1920×1080, 1080×1920 and 3840×2160, plus the admin modal, the unsaved-settings
  preview and the aircraft detail tap.

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
