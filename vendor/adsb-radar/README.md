# ADS-B Radar aircraft icons (vendored)

Source: **ADS-B Radar for macOS** — <https://adsb-radar.com/help/icons.html>
Package: `ADS-B_Radar_Free_Aircraft_SVG_Icons.zip` (37 top-down aircraft silhouettes).

License, verbatim from the package's `Readme and Attribution.rtf`:

> These aircraft SVG icons are free to use for personal and commercial projects.
> The only requirement: please provide a backlink to ADS-B Radar somewhere in your
> project, website, or documentation - or buy the App ADS-B Radar 
>
> Example attribution:
> Icons by ADS-B Radar for macOS - https://adsb-radar.com - https://apps.apple.com/app/id1538149835

**Where the required backlink lives in this repo:** `README.md` ("Aircraft icons"),
`DOCS.md` ("Aircraft icons and the single-aircraft dashboard") and the credit line of both
the radar display and the single-aircraft dashboard, which print
`Aircraft icons: ADS-B Radar (adsb-radar.com)`.

## Why these

The app needs a *correct schematic per aircraft type*. No free, offline, per-ICAO-type image
database exists for this: Flightradar24 only publishes airline logos and jetphotos photos, and
every "aircraft type" API found is either keyed on nothing stable, paid, or not licensed for
redistribution. This set is the closest thing — 37 real top-down silhouettes covering the
airliner families (A320/A330/A340/A380, B737/B747/B767/B777/B787, MD-11), regional (CRJ, ERJ,
E-Jet, F100, Dash 8), business (Falcon, Gulfstream, Learjet), light (Cessna, light twins) and
the non-fixed-wing categories (helicopter, glider, hang glider, drone).

They are matched to the sensor's `aircraft_code` (ICAO type designator) at **family** level:
`A321`/`A20N` → the A320 icon, `B38M`/`B739` → the B737 icon, `AW189`/`EC35` → the helicopter
icon. That mapping is `AIRCRAFT_ICONS` in `app.py`.

## Files

* `*.svg` — the icons exactly as shipped (do not hand-edit; they are re-normalised, not
  rewritten, by `tools/build_aircraft_icons.py`).
* `Readme and Attribution.rtf` — the original package readme, kept verbatim.

`tools/build_aircraft_icons.py` converts these into `web/aircraft_icons.json`, which `app.py`
inlines into the display page (so the kiosk never fetches an image).