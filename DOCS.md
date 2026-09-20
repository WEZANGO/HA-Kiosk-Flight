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
| View range (km) | `0` = automatic: fits the sensor's box **and** the furthest aircraft, so nothing is ever drawn off-screen |
| Refresh every | seconds; the display polls the app, the app caches HA state for 5 s |
| Origin → destination, Aircraft type, Speed, Distance from the centre | label content |
| Hide aircraft on the ground | default on |
| Flight trails, Range rings, Radar sweep | cosmetics |
| Centre | `zone.home` or custom coordinates |
| Accent colour | the aircraft glyph, since the compass can never be |

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

What the app does **not** do: it does not call Flightradar24 itself. If the sensor is empty the
display says *No aircraft overhead*, which is the normal state most of the time — the sensor's
area is a box a few kilometres across. Widen the radius on the *integration*, and this app follows.

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
| "No aircraft overhead" | the sensor's area is empty right now; the admin page's *Aircraft in the area right now* line says how many there are |
| "Reading Home Assistant entities needs the homeassistant_api permission" | the app is running outside Home Assistant, or `homeassistant_api: true` was removed from `config.yaml` |
| Display shows a 401 | direct URL without `?auth=…` — copy the link from the admin page instead of typing it |

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
