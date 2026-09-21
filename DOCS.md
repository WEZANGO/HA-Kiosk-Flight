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

There is also a second, full-screen view for **one** aircraft — the **single-aircraft dashboard**:

```
              ┌────┐
              │ EI │  Aer Lingus          ← airline badge (its own code, coloured per airline)
              └────┘
              EI725                       ← flight number
                ✈                         ← top-down schematic of the aircraft TYPE
           Airbus A320-251N  A20N
              ALTITUDE
               777 m
          LHR   →   ORK                   ← origin → destination
        London       Cork
```

It can *be* a display (a standalone dashboard), or the radar can hand over to it when the sky
has emptied down to a single aircraft, or when an aircraft is tapped. Those three are
independent settings, all off by default.

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
| `image_library` | folder the *Aircraft image* setting reads pictures from (default `/data/liveries`) |
| `image_api_url` | optional URL template for a picture API — `{icao}`, `{type}` and `{key}` are substituted. Empty = library only |
| `image_api_key` | the key for that API (stored as a password field in Home Assistant; never in this repository) |

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
| Flight trails, Range rings, Radar sweep, Home-marker ripple | cosmetics |
| Home-marker ripple | the slow expanding ring at the home marker. It expands with a composited transform, is invisible at both ends of its loop and stays invisible for the first 10% of each cycle, so the restart cannot twitch; turn it off entirely if you prefer a still marker |
| Centre | `zone.home` or custom coordinates |
| Accent colour | drives the aircraft glyphs, trails, range rings, compass, the sweep, the home-marker glow **and the dashboard's schematic/arrow/glow** (the nearest aircraft is drawn in a lightened version of it). The airline badge keeps its own per-airline colour, because that one identifies the airline |
| Text sizes | eight sliders — overhead count, headline, info lines, aircraft callsign, aircraft detail lines, compass & area labels, single-aircraft dashboard, credit line — each 50–300% of the design size, applied per element. The dashboard slider scales its whole layout in one move |
| **Single-aircraft dashboard** — *This display IS the dashboard* | the display shows nothing but the dashboard, full screen (a standalone dashboard) |
| **Single-aircraft dashboard** — *Switch to it when only one aircraft is left* | the radar hands over automatically when exactly one aircraft is in the area, and comes back when a second appears |
| **Single-aircraft dashboard** — *Open it when an aircraft is tapped* | a tap opens the dashboard for **that** aircraft instead of the bottom detail strip; a tap on the dashboard returns to the radar. If the tapped aircraft leaves the area, the radar comes back |
| **Airline logo** | *The airline's own logo* — the **app** fetches it once per airline (the kiosk never does), keeps it under `/data/logos` and embeds it in the page; *Monogram badge* — the airline's code on a coloured square, nothing fetched at all |
| **Aircraft type graphic** | *Top-down* — the familiar plan view (37 silhouettes, per family); *Side view* — a profile of the type (9 profiles, per class: twin-jet, widebody, four-engine, regional, business jet, turboprop, light, helicopter, glider). Both sets are local; see "Aircraft icons and the type schematic" |
| **Aircraft image** | *Drawn silhouette* (default) — nothing fetched, ever; *Real picture* — a picture of **this airline on this aircraft type**, looked up in the image library and/or a keyed API configured in the app options, with the silhouette as the fallback. See "Aircraft pictures" |

All three dashboard switches are off by default, so an existing display never changes behaviour on
upgrade. Ticked together they combine: a display that is a dashboard, whose radar returns whenever
the sky fills up again.

### The single-aircraft dashboard

One aircraft, the whole screen, refreshed on the same interval as the radar:

* **the airline** — its own **logo** on a light plate when the display's *Airline logo* setting is
  on, and then nothing else: the logo already says who the airline is, so the name only appears when
  the badge is a bare code (setting off, logo not fetched yet, or the airline has no logo file). See
  "Airline logos" below;
* **the flight number** (`flight_number`, falling back to the callsign);
* **a top-down schematic of the aircraft type** (below), or a **side profile** if the display is set
  that way — the same slot, sized for the drawing it is holding;
* **the aircraft type** as text (`aircraft_model` with the ICAO code beside it) — the largest line
  under the schematic;
* **the altitude**, in the display's units, smaller than the type — or `ON THE GROUND` for a taxiing
  aircraft;
* **origin → destination**, IATA codes with the city underneath.

The dashboard has **no footer and no credit line**: it is the whole screen for one aircraft. (The
radar keeps its own `Flightradar24 via Home Assistant` credit line, and the attribution the icon
licence asks for lives in the docs and the admin page — see "Aircraft icons".)

Which aircraft it shows: the **closest** one (by distance from the display's centre) — unless it was
opened by a tap, in which case it follows that aircraft until it leaves the area. The radar's sort
order does not change this: "closest" is the point of the view.

No aircraft in the area, and the dashboard says so rather than showing empty fields; if the sensor
itself is broken (missing entity, no `flights` list), the dashboard prints the reason — a display
set to be a dashboard must never fail silently.

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
* the aircraft **schematics** are vendored artwork, inlined into the page as JSON by the app
  (`web/aircraft_icons.json`) rather than linked as `<img src>` — so the same page also renders
  correctly from the admin's `srcdoc` preview, which has no base URL to fetch from.
* the airline is a **monogram badge**, coloured from the airline's own code: a real logo would be
  one more third-party fetch (and a trademark question) for a display that needs neither. (v0.2.1
  added the real logo as an option — see "Airline logos": the *app* fetches it once, the kiosk
  still fetches nothing, and the monogram stays as the fallback and the off-switch.)

What the app does **not** do: it does not read live flight data from Flightradar24 (only the
Supervisor API — the integration owns that session), and it cannot widen what the sensor reports.
Its one outbound request is the airline logo artwork, and that is opt-out per display.

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
`airline_iata`/`airline_icao`, `airport_origin_code_iata`, `airport_destination_code_iata`
(+ city names), `latitude`, `longitude`, `altitude` (ft), `heading` (deg), `ground_speed` (kt),
`vertical_speed` (ft/min), `on_ground`, `coordinates` (trail), and `bounds` on the sensor for the
automatic range. `aircraft_category` decides the schematic class for anything that is not a
fixed-wing aeroplane (helicopter, glider, balloon, drone).

Distances and bearings are recomputed here from the display's own centre, so a display centred
somewhere else (not `zone.home`) is still correct.

## Aircraft icons and the type schematic

The dashboard draws a silhouette of the aircraft **type**, matched from the ICAO type designator the
sensor already reports (`aircraft_code`: `A320`, `B38M`, `AW189`, …), in either of two sets — the
display's *Aircraft type graphic* setting:

| | Top-down (default) | Side view |
|---|---|---|
| What it is | the plan view, as if looking down | a profile, as if standing beside it |
| How many | 37 silhouettes | 9 profiles |
| Matched by | **family**: `A321`/`A20N` → the A320 icon, `B38M`/`B739` → the 737 | **class**: twin-jet, widebody, four-engine, regional, business jet, turboprop, light aircraft, helicopter, glider |
| Best for | showing what is overhead and where it is pointing | showing what the aircraft *is* |

A profile distinguishes aircraft by class — engine count, wing position, propellers, tail — so an
A321 and a 737 share one; a 777 and a 787 share the heavier one; a 747, an A340 and an A380 share
the four-engine one. Anything with no profile (a drone, a balloon, a hang glider, an airport
vehicle) keeps its plan view rather than being handed a wrong profile.

### Where the artwork comes from

There is no free, offline, per-type image database to look this up in — Flightradar24 publishes
airline logos and photographs, neither of which answers "what does an A321 look like from above" —
so the app **vendors** its artwork:

**Top-down set (37 silhouettes)** — ADS-B Radar for macOS:

> Icons by **ADS-B Radar** for macOS — <https://adsb-radar.com> —
> <https://apps.apple.com/app/id1538149835>
>
> Free for personal and commercial use; the requirement is the backlink above, which lives in
> `README.md`, in this file and on the app's own admin page. The **radar** display's credit line is
> unchanged (`Flightradar24 via Home Assistant`); the dashboard has none, deliberately — a
> full-screen dashboard with a footer is the thing this was asked to stop being.

**Side-view set (9 profiles)** — two converted from **sisl/aircraftshapes** (MIT, Stanford
Intelligent Systems Laboratory — `vendor/aircraftshapes/`, licence kept verbatim), the other seven
authored in this repository in the same flat style. Full provenance per file:
`vendor/side-views/README.md`.

### How it is wired

* Artwork: `vendor/adsb-radar/*.svg` and `vendor/side-views/*.svg` (both unmodified as vendored;
  the side set is generated by `tools/build_side_views.py`).
* Build step: `python3 tools/build_aircraft_icons.py` folds both sets into
  `web/aircraft_icons.json` (strips editor grid guides, recolours every fill/stroke to
  `currentColor`, minifies). **Re-run it after changing any icon**, and re-run
  `tools/build_side_views.py` first if the profiles themselves changed. The app reads that JSON at
  startup and inlines it into the display page, so the kiosk never fetches an image and the admin's
  `srcdoc` preview works too.
* Matching lives in `app.py`: `ICON_BY_CODE` / `ICON_BY_CATEGORY` / `ICON_BY_MODEL` pick the plan
  view (type designator → category → the words in `aircraft_model` → a generic twin-jet airliner,
  `a5`), and `SIDE_BY_ICON` maps that onto a profile.
* Airport **ground vehicles** (`GRND`) get no schematic on purpose: drawing an aeroplane for a
  fuel truck would be a lie, so the dashboard falls back to the plain plane glyph.

If your area turns up a type that is obviously mismatched, add its designator to the table in
`app.py` and mention it — the table is meant to grow.

## Aircraft pictures (per airline, per aircraft type)

*Aircraft image → Real picture* replaces the drawn silhouette with a picture of **this airline on
this aircraft type** — a Ryanair 737-800 livery for a Ryanair 737-800. It is looked up by
`<ICAO>_<TYPE>` (e.g. `EIN_A21N`: Aer Lingus, A321neo).

Two sources, tried in this order:

1. **A keyed API**, if `image_api_url` is set in the app options — a URL template with `{icao}`,
   `{type}` and `{key}` substituted (`image_api_key` supplies `{key}`). This is the shape most
   aviation-asset APIs use; the headers sent are `x-api-key` and `Authorization: Bearer`, because
   providers differ and neither choice should be the user's problem.
2. **A local image library** — `image_library` (default `/data/liveries`), read as
   `<ICAO>_<TYPE>.jpg` (or `.png`, `.webp`, `.svg`), or with any filename at all if an `index.json`
   in that folder maps keys to files: `{"EIN_A21N": "aer-lingus-a321.jpg"}`.

Whatever is found is cached under `/data` and embedded in the page as a `data:` URI, so the kiosk
still fetches nothing and the admin preview still works. With no picture — not configured, nothing
found, or a failed download — the drawn silhouette is shown, so a display never comes up empty.

**Nothing of this is in the repository.** The library is the user's own material (licensed artwork
they bought, or their own exports), the API key is Home Assistant configuration, and the repo
contains no raster artwork at all — a check in the test suite asserts that, and that the library
path is outside the repo, so this stays true by accident-proof.

The same fetch rules as the airline logos apply, because they share one implementation: never block
a poll on the network, remember failures (one miss for an hour; three network errors stands the
fetcher down for half an hour with one log line), and treat a 4xx as "no picture for this aircraft"
rather than "the internet is down".

**Where to get pictures** (researched, for the record):

| Source | What it is | Verdict |
|---|---|---|
| Aviation asset APIs (e.g. logostream) | keyed API, airline logos on a free tier, **aircraft liveries on a paid tier**; `x-api-key`, dark-mode variants, CDN | **works with the `image_api_url` template** — this is what the feature is shaped for |
| Stock illustration shops (e.g. NorebboStock) | per-airline/per-type illustrations, ~$12 each, in Shopify; the public catalogue exposes ~530 products with unwatermarked preview images | buy a licence, then point `image_library` at the purchased files; the previews are for evaluating the look, not for use |
| FSLTL / AIG (flight-sim traffic packs) | 2,375 liveries matched per airline+type — the right taxonomy, the wrong format: 3D glTF models + 4,032 DDS textures, **71 raster images in the whole 10 GB repo, no open licence at all** | **not usable**: sim-use-only freeware with no licence file, and nothing to harvest as an image |

The airline logos and the aircraft pictures are the airlines' and photographers'/artists' own marks
and work. This app is for a personal, non-commercial display.

## Airline logos

The dashboard's *Airline logo* setting (on by default) shows the airline's own logo. It comes from
Flightradar24's operator logo set, which is keyed on the airline's **ICAO** code — `EIN_logo0.png`
exists, `EI_logo0.png` does not — so the lookup key and the two-letter badge code are different
fields (`airline_icao` vs `airline_iata`).

This is the app's only outbound request, and it is built so that it can never cost you a display:

* **the kiosk never fetches anything.** The app fetches on the machine that runs it and hands the
  page a `data:` URI, so the same page still renders in the admin's `srcdoc` preview and keeps
  working after the network has gone away.
* **nothing waits for the network.** A poll answers with whatever is cached and asks a background
  thread for the rest, so a slow or dead CDN costs the **code badge for a few seconds**, never a
  stalled display. The logo appears on a following poll.
* **the artwork is kept forever, in `/data/logos/<ICAO>.png`.** One download per airline, ever;
  the cache survives restarts and upgrades, and is served from disk after that.
* **only airlines this display shows are fetched** — never the sensor's whole area (this sensor
  reports 281 aircraft and some 65 airlines; a 6-aircraft display asks for at most 6).
* **failures are remembered.** A miss is not retried for an hour, and after three network failures
  (a Home Assistant with no internet) the whole fetcher stands down for half an hour and logs
  `airline logos unreachable` — the code badge is used throughout, and nothing retries per poll.
* **a 404 is not a failure.** Airlines without a logo file (14 of this sensor's 65) simply keep the
  code badge; that is not counted as the internet being down.

Turn it off per display with *Airline logo → Monogram badge* if you would rather this app never
touched the internet at all; the payload then carries no logo bytes whatsoever.

**Why the light plate:** the logos are the airlines' own wordmarks, and several of them (British
Airways, American, Air France) are dark navy — on a night-sky background they would be nearly
invisible. They sit on a soft off-white plate, which is also what the coloured code badge became, so
the two fallbacks are visually the same object. Because the logo *is* the airline's identity, the
name beside it is hidden while a logo is shown — it comes back automatically when the badge is only
a code.

**Trademark note:** these are the airlines' own marks, published by Flightradar24 alongside its
data, used here for a personal non-commercial display (the radar view credits Flightradar24 in its
footer). If you ever republish this app for others, that is the part to re-check first.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Home Assistant has no entity sensor.…" | the sensor ID is wrong, or the Flightradar24 integration is not installed |
| "… has no 'flights' list" | that entity is not an *in area* sensor (an airport sensor reports schedules without positions) |
| "No aircraft in the sensor area" | nothing airborne inside the sensor's own area (now stated on screen, with that area's size). Widen the area in the Flightradar24 integration — `Radar range` here is zoom only |
| Widened the area and still nothing | check the footer: `N on the ground hidden` means every flight in the area is taxiing, and `0 airborne of 0` means the box is genuinely quiet. The dashed box on the scope shows exactly how far the sensor looks |
| "Reading Home Assistant entities needs the homeassistant_api permission" | the app is running outside Home Assistant, or `homeassistant_api: true` was removed from `config.yaml` |
| Display shows a 401 | direct URL without `?auth=…` — copy the link from the admin page instead of typing it |
| "Display error" on screen | a client-side render failure. It is shown deliberately: a swallowed error used to leave a blank scope with no explanation. On a display set to be a dashboard the radar is brought forward to show it, because the dashboard covers the notice |
| The dashboard shows "NO AIRCRAFT IN THE AREA" | exactly that: nothing airborne in the sensor's own area (the same condition as the empty radar, which prints the full explanation) |
| The dashboard shows a twin-jet airliner for something odd | the type is not in the icon table and nothing in the category or model matched, so `a5` is the deliberate last resort — add the designator to `ICON_BY_CODE` in `app.py` |
| Side view shows an airliner for a 747 | that is by design at *class* level: `quad` covers 747/A340/A380, `heavy` covers 777/787/A330/767, `jet` covers A320/737. Nine profiles cannot be thirty-seven families — switch back to *Top-down* if you want the family silhouette |
| A drone or balloon still shows a plan view in side-view mode | deliberate: there is no profile for them, so the plan view is kept rather than showing a wrong profile |
| Tapping an aircraft opens a different one's dashboard | the tap landed on a label that overlaps the glyph; the label is what is on top, so it is what opens |
| The dashboard is on screen for the wrong aircraft | it shows the **closest** aircraft unless it was opened by a tap; a tapped aircraft is followed until it leaves the area |
| No airline logo, just the code badge | three normal cases: the logo has not been fetched yet (it appears on a later poll), the airline has no logo file at Flightradar24 (14 of the 65 airlines this sensor sees), or the display is set to *Monogram badge*. A *Home Assistant with no internet* logs `airline logos unreachable` once and then stops trying for 30 minutes |
| The logo is a broken image | it cannot be: the page is given a `data:` URI and falls back to the code badge when there is none. If you see a broken icon, the payload is being rewritten by something in front of the app |
| Fetching logos is not wanted at all | set *Airline logo → Monogram badge* on each display; the app then makes no outbound request for that display (and sends no logo bytes) |
| No aircraft picture, just the silhouette | normal: nothing configured (`image_api_url` empty and the library empty), nothing found for this airline+type, or the fetch failed. With *Real picture* set, the app asks for `<ICAO>_<TYPE>` — check the library has that exact name, or that the `index.json` maps it |
| Pictures are the wrong airline or type | whatever is in the library wins over what is "right": the app matches on the file name (`EIN_A21N`) and does not inspect the image. Remove or rename the file to fix it |
| A picture API returns nothing | check `image_api_url`'s placeholders (`{icao}`, `{type}`, `{key}`), that the key is set, and the log line `aircraft images unreachable` — after three network errors the fetcher stands down for half an hour by design |

## Design rules this app follows

* Single `app.py` (Python stdlib only) + `web/display.html` (both views) + `web/aircraft_icons.json`
  (generated) + `run.sh` + `config.yaml` + `Dockerfile` + `repository.yaml`, with the icon artwork
  and its build script under `vendor/` and `tools/` (neither ships in the image).
* Full-screen only — no card variant, and one flat settings set (no per-variant fields). The
  single-aircraft dashboard is a *view of the same display*, not a second display type: it inherits
  the display's sensor, units and refresh interval.
* Pages are served `Cache-Control: no-store`: a cached display page is exactly what keeps a kiosk
  stuck on old client code.
* Client JS stays conservative (ES2017 at most): `??`, `?.` and `matchAll` kill the whole script on
  an old kiosk WebView, not just their own line. No external resources, so nothing to fail — the
  aircraft schematics are inlined into the page for the same reason.
* Third-party artwork is vendored with its licence and its required attribution; the generated file
  (`web/aircraft_icons.json`) is committed so the image builds without the build step ever running.
* Version bumped in **both** `config.yaml` and the Dockerfile `io.hass.version` label on every
  change.
