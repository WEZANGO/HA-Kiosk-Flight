# Kiosk Flight Displays

A Home Assistant app (add-on) that shows **the aircraft flying over your house**, full screen, on a
kiosk. It reads the aircraft list that Home Assistant's
[Flightradar24 integration](https://github.com/AlexandrErohin/home-assistant-flightradar24) already
publishes on its *in area* sensor, and draws a local radar: no map tiles, no SDK, no external
requests from the kiosk.

```
       4  ← aircraft overhead right now
AIRCRAFT OVERHEAD
nearest 5.8 km
highest 10,973 m
range 35 km
                    N
            ╭───────────────╮
         20 km     ✈ RYR3NL ▼      ← callsign, altitude, distance, speed, route
            │   2,347 m · 8.7 km · 504 km/h
            │        BCN → ORK
            │      ● HOME
            ╰───────────────╯
                 ✈ EI-HAP
```

Tap any aircraft to get its details (registration, type, route, exact altitude/speed/bearing) in a
line at the bottom.

---

## Install

1. The repo is a Home Assistant **app repository**: Settings → Add-ons → Add-on store → ⋮ →
   *Repositories*, add
   `https://github.com/<owner>/kiosk-flight-displays` (or the folder path if it is a local repo),
   then install **Kiosk Flight Displays**.
2. You need the **Flightradar24** integration already configured in Home Assistant, with a sensor
   tracking your area — `sensor.flightradar24_current_in_area` is the default. Nothing else: no API
   key goes into this app, and no key ever reaches the kiosk.
3. Start the app, open it from the sidebar (*Kiosk Flight*), press **Add a full-screen flight
   display**, and copy the **Direct** link onto the kiosk.

The app asks the Supervisor for its permissions:

| Permission | Why |
|---|---|
| `ingress: true` | the admin page and displays inside the HA frontend |
| `homeassistant_api: true` | read `sensor.flightradar24_current_in_area` (and `zone.home`) through the Supervisor API |
| port `8096/tcp` | the direct, token-authenticated URL kiosks use when they have no HA session |

## The three URLs

| Link | Use |
|---|---|
| **Ingress** — `/display/<id>` | relative, no token; works inside Home Assistant (Webpage card, sidebar) |
| **Direct** — `http://<host>:8096/display/<id>?auth=…` | kiosks, tablets, anything without an HA login |
| **Preview** (button in the editor) | renders the *unsaved* form, so you can look before you save |

The shared access token lives in `/data/access_token` — deliberately **not** in
`/data/options.json`, which Home Assistant rewrites from the app's configuration on every restart
(that would regenerate the token and break every saved kiosk link).

## Settings

**App options** (Configuration tab, `/data/options.json`):

| Option | Meaning |
|---|---|
| `flight_entity` | default sensor for new displays (default `sensor.flightradar24_current_in_area`) |
| `home_latitude` / `home_longitude` | optional fixed centre; `0` means "use `zone.home`" |

**Per display** (stored in `/data/flight_displays.json`):

| Setting | Notes |
|---|---|
| Name | required; becomes the ID in the URL |
| Title shown on screen | replaces "AIRCRAFT OVERHEAD" |
| Sensor | every sensor in HA that carries a *positioned* `flights` list, live count shown |
| Aircraft shown | 1–20, nearest first by default |
| Sort by | nearest / lowest / highest / fastest / callsign |
| Units | Metric (km, m, km/h) · Aviation (nm, ft, kt) · Imperial (mi, ft, mph) |
| View range (km) — *"Radar range"* | `0` = automatic: fits the sensor's box **and** the furthest aircraft, so nothing is ever drawn off-screen. **Zoom only** — it cannot bring in aircraft the sensor does not report. |
| Refresh every | seconds; the display polls the app, the app caches HA state for 5 s |
| Origin → destination, Aircraft type, Speed, Distance from the centre | label content |
| Hide aircraft on the ground | default on |
| Flight trails, Range rings, Radar sweep | cosmetics |
| Centre | `zone.home` or custom coordinates |
| Accent colour | drives the aircraft glyphs, trails, range rings, compass, the sweep and the home-marker glow (the nearest aircraft is drawn in a lightened version of it) |
| Text sizes | seven sliders — overhead count, headline, info lines, aircraft callsign, aircraft detail lines, compass & area labels, credit line — each 50–300% of the design size, applied per element |

The screen itself keeps to: the aircraft count and info lines top-left, the scope, and a single
credit line (`Flightradar24 via Home Assistant`) bottom-right. There is no timestamp, no
entity name and no progress bar on the display — the scope's dashed box already shows the
sensor's coverage and the count already shows how many are up.

## How it works

```
Flightradar24 integration ──▶ sensor.flightradar24_current_in_area (flights: [ … ])
                                        │  Supervisor API  http://supervisor/core/api
                                        ▼
                              this app (app.py, stdlib only)
                    distance + bearing computed per aircraft (haversine / great-circle)
                                        │  JSON: polar coordinates, no trig on the client
                                        ▼
                     kiosk page (web/display.html): local SVG radar, no network at all
```

Everything external is resolved **inside** Home Assistant, so a kiosk on a VLAN with no internet
needs nothing but this app:

* the aircraft data arrives through the Supervisor API (local);
* the page is served by the app (local);
* the radar, glyphs, fonts and colours are all local — **the display page makes zero external
  requests**. There is deliberately no generic proxy endpoint: there is nothing to proxy, and an
  unused open relay is a liability.

What the app does **not** do: it does not call Flightradar24 itself, and it cannot widen
what the sensor reports.

## What decides which aircraft appear

This is the question that matters, because it is **not** a setting in this app:

```
Flightradar24 integration (HA)          this app
├─ area/radius  ── which box is polled ─┐
├─ min/max altitude (defaults: no limit)│  ─→  tailable filters  ─→  display
└─ its own data source (free/paid)      ┘        (hide ground aircraft,
                                                  aircraft shown, sort)
```

* **The area is the integration's.** With `100` set on the integration the sensor's box is
  100 km × 100 km (≈ ±50 km each way) — the display prints that as `area ≈50 km` in the
  footer and draws the box as a dashed outline on the scope. Aircraft outside it are never
  reported, so widening *this app's* "Radar range" cannot reveal them.
* **The scope shows where the boundary is.** The dashed box on the radar is the sensor's
  own coverage; the whole reason it exists is so "nothing is showing" is never a mystery.
* **The integration also filters.** It publishes `on_ground` for taxiing aircraft and its
  own min/max altitude window. This app additionally hides aircraft on the ground by
  default ("Hide aircraft on the ground"), and the footer says how many it hid
  (`2 on the ground hidden`).
* **`Aircraft shown` (1–20)** is only a display limit, applied after sorting; the footer
  and the HUD count always report how many are actually there (`2 airborne of 4`).

So an empty display means one of: the sensor's area is quiet, the sensor is unavailable, or
the only aircraft in it are on the ground. All three are visible on the page now.

## Flight data used

Per aircraft, from the sensor's `flights` attribute:
`callsign`, `flight_number`, `aircraft_registration`, `aircraft_code`/`aircraft_model`, `airline`,
`airport_origin_code_iata`, `airport_destination_code_iata` (+ city names), `latitude`, `longitude`,
`altitude` (ft), `heading` (deg), `ground_speed` (kt), `vertical_speed` (ft/min), `on_ground`,
`coordinates` (trail), and `bounds` on the sensor for the automatic range.

Distances and bearings are recomputed here from the display's own centre, so a display centred
somewhere else (not `zone.home`) is still correct.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Home Assistant has no entity sensor.…" | the sensor ID is wrong, or the Flightradar24 integration is not installed |
| "… has no 'flights' list" | that entity is not an *in area* sensor (an airport sensor reports schedules without positions) |
| "No aircraft in the sensor area" | nothing airborne inside the sensor's own area (now stated on screen, with that area's size). Widen the area in the Flightradar24 integration — `Radar range` here is zoom only |
| Widened the area and still nothing | check the footer: `N on the ground hidden` means every flight in the area is taxiing, and `0 airborne of 0` means the box is genuinely quiet. The dashed box on the scope shows exactly how far the sensor looks |
| "Reading Home Assistant entities needs the homeassistant_api permission" | the app is running outside Home Assistant, or `homeassistant_api: true` was removed from `config.yaml` |
| Display shows a 401 | direct URL without `?auth=…` — copy the link from the admin page instead of typing it |
| "Display error" on screen | a client-side render failure. It is shown deliberately: a swallowed error used to leave a blank scope with no explanation |

## Design rules this app follows

* Single `app.py` (Python stdlib only) + `web/display.html` + `run.sh` + `config.yaml` +
  `Dockerfile` + `repository.yaml`.
* Full-screen only — no card variant, and one flat settings set (no per-variant fields).
* Pages are served `Cache-Control: no-store`: a cached display page is exactly what keeps a kiosk
  stuck on old client code.
* Client JS stays conservative (ES2017 at most): `??`, `?.` and `matchAll` kill the whole script on
  an old kiosk WebView, not just their own line. No external resources, so nothing to fail.
* Version bumped in **both** `config.yaml` and the Dockerfile `io.hass.version` label on every
  change.
