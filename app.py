"""Kiosk Flight Displays — a Home Assistant app showing the aircraft flying over the house.

Data comes from a Home Assistant sensor that carries a ``flights`` attribute list
(the Flightradar24 HACS integration's ``sensor.flightradar24_current_in_area``),
read through the Supervisor API. Everything is resolved inside Home Assistant, so
a kiosk on a VLAN with no internet needs nothing but this app: no map tiles, no
SDK, no external API, no key on the device.

The single exception is the airline logo artwork on the single-aircraft dashboard:
that one is fetched by THIS app (never by the kiosk), once per airline, cached
under /data, and can be switched off per display ("Airline logo: code badge").
Your own file for an airline, dropped in the logo folder (or added on the admin
page), wins over the fetched one — and any solid backing plate in fetched artwork
is repainted white. See the logo sections below for the rules they follow.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

# Module constants: a local test harness rebinds these before serving (see DOCS.md).
DATA_FILE = Path("/data/flight_displays.json")
OPTIONS_FILE = Path("/data/options.json")
DISPLAY_FILE = Path("/app/web/display.html")
PORT = 8096

# The Supervisor proxy, used with the token the add-on gets automatically when
# `homeassistant_api: true` is set in config.yaml. No user token anywhere.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()
SUPERVISOR_API = "http://supervisor/core/api"

OPTION_DEFAULTS = {
    "flight_entity": "sensor.flightradar24_current_in_area",
    "home_latitude": 0.0,
    "home_longitude": 0.0,
    # Aircraft-picture sources for the dashboard's "Aircraft image" setting. Both
    # are the user's own configuration, held in Home Assistant's options file —
    # never in the repository. Empty api_url = library only.
    "image_library": "/data/liveries",
    "image_api_url": "",          # e.g. https://…/livery?icao={icao}&type={type}&key={key}
    "image_api_key": "",
    # Your own airline logos. Any image file in here answers for the airline whose
    # name it carries ("Aer Lingus.png") or whose code it carries ("EIN.png"), and
    # wins over the artwork the app would have fetched. Fetched artwork lives in a
    # `.fetched` sub-folder, so what is in this folder is always yours.
    "logo_library": "/data/logos",
}

# Every display setting. Booleans travel FormData -> JSON -> injected JS as the
# strings "true"/"false"; they are read back tolerantly (String(v) !== "false").
DEFAULTS = {
    "entityId": "",                 # empty -> the app option, then the FR24 default
    "centreMode": "home",           # "home" (HA zone.home) | "custom"
    "latitude": "",
    "longitude": "",
    "rangeKm": "0",                 # 0 = automatic
    "maxFlights": "6",
    "sortBy": "nearest",            # nearest | lowest | highest | fastest | callsign
    "units": "metric",              # metric | aviation | imperial
    "hideOnGround": "true",
    "showRoute": "true",
    "showType": "false",
    "showSpeed": "true",
    "showDistance": "true",
    "showTrails": "true",
    "showRings": "true",
    "showSweep": "true",
    "showPulse": "true",
    "title": "",
    "accent": "#7dd3fc",
    "refreshInterval": "20",
    # The single-aircraft dashboard. `detailAlways` makes this display BE the
    # dashboard (standalone); the other two are the optional ways in from the
    # radar — automatically when the sky empties down to one aircraft, or on tap.
    "detailAlways": "false",
    "detailAuto": "false",
    "detailClick": "false",
    # What the airline part of the dashboard shows: the real logo (your own file
    # for the airline if there is one, else fetched once by the app on the machine
    # that runs it — never by the kiosk — and kept on disk) or the code badge,
    # which needs nothing but the code.
    "airlineLogo": "image",
    # How the aircraft type is drawn: "top" (the familiar plan view) or "side"
    # (a profile). Both sets are vendored and inlined; see the icon section.
    "typeGraphic": "top",
    # Where the type graphic comes from: the vendored silhouettes ("silhouette"),
    # or a real picture of the airline's livery on that type ("image"), looked up
    # in the image library and/or a keyed API configured in the app options, with
    # the silhouette as the fallback.
    "typeImage": "silhouette",
    # Per-element text sizes, as a percentage of the design size (100 = as designed).
    "sizeCount": "100",
    "sizeTitle": "100",
    "sizeInfo": "100",
    "sizeCallsign": "100",
    "sizeDetails": "100",
    "sizeFooter": "100",
    "sizeGrid": "100",
    "sizeDetail": "100",
}
SIZE_KEYS = ("sizeCount", "sizeTitle", "sizeInfo", "sizeCallsign", "sizeDetails",
             "sizeFooter", "sizeGrid", "sizeDetail")
DETAIL_FLAGS = ("detailAlways", "detailAuto", "detailClick")
LOGO_KEYS = ("image", "monogram")
TYPE_GRAPHIC_KEYS = ("top", "side")
TYPE_IMAGE_KEYS = ("silhouette", "image")
SORT_KEYS = ("nearest", "lowest", "highest", "fastest", "callsign")
UNIT_KEYS = ("metric", "aviation", "imperial")
CENTRE_KEYS = ("home", "custom")

STATE_TTL = 5.0         # seconds a Supervisor API reply is reused
ZONE_TTL = 300.0        # seconds zone.home coordinates are reused
STATE_LOCK = threading.Lock()
_STATE_MAP: dict = {}
_STATE_MAP_AT = 0.0
_ENTITY_CACHE: dict = {}   # entity_id -> (fetched_at, state)
_ZONE_AT = 0.0
_ZONE_POINT = None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def as_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def as_text(value, limit=120):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def truthy(value, default: bool = True) -> bool:
    """Tolerant boolean reader.

    Display settings arrive as the strings "true"/"false" (FormData -> JSON -> JS),
    but the same helper is used for booleans coming out of Home Assistant, where the
    Flightradar24 integration publishes `on_ground` as an INTEGER 0/1. Reading that
    with a string-only test made 0 look truthy, which hid every aircraft on the
    display: handle bool, number and string explicitly.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("false", "0", "no", "off", "none")


def options() -> dict:
    """Add-on level options (HA-owned /data/options.json), read defensively."""
    try:
        stored = json.loads(OPTIONS_FILE.read_text())
        return stored if isinstance(stored, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def option(key: str):
    value = options().get(key, OPTION_DEFAULTS.get(key))
    return OPTION_DEFAULTS.get(key) if value in (None, "") else value


def access_token() -> str:
    """Shared access token for direct (non-ingress) connections.

    Lives in the app's OWN file (/data/access_token), NOT options.json: Home
    Assistant rewrites options.json from the add-on configuration on every
    restart, which would regenerate the token and break every saved kiosk link.
    """
    token_file = OPTIONS_FILE.parent / "access_token"
    try:
        token = token_file.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = token_file.with_suffix(".tmp")
        temporary.write_text(token + "\n")
        temporary.replace(token_file)
    except OSError:
        return ""
    return token


# --------------------------------------------------------------------------- #
# Home Assistant state (Supervisor API proxy)
# --------------------------------------------------------------------------- #
def supervisor_states() -> dict:
    """Every entity state, keyed by entity_id, cached for a few seconds.

    Only the admin page's sensor list needs the whole map; the display reads its one
    sensor (and zone.home) through entity_state() instead.
    """
    global _STATE_MAP, _STATE_MAP_AT
    with STATE_LOCK:
        if _STATE_MAP and time.time() - _STATE_MAP_AT < STATE_TTL:
            return _STATE_MAP
    if not SUPERVISOR_TOKEN:
        raise ValueError("Reading Home Assistant entities needs the homeassistant_api "
                         "permission and must run as a Home Assistant app.")
    request = Request(f"{SUPERVISOR_API}/states",
                      headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                               "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            listed = json.load(response)
    except HTTPError as error:
        raise ValueError(f"Home Assistant API returned HTTP {error.code}.") from None
    except (URLError, OSError):
        raise ValueError("Home Assistant API is unreachable.") from None
    mapping = {item.get("entity_id", ""): item for item in listed if item.get("entity_id")}
    with STATE_LOCK:
        _STATE_MAP, _STATE_MAP_AT = mapping, time.time()
    return mapping


def entity_state(entity_id: str):
    """One entity's state, fetched directly and cached briefly.

    The display polls about every 20 s and needs exactly two entities. Reading every
    state on the instance (1 MB on this one, almost all of it the Flightradar24
    `flights` attributes) to get two of them is wasteful on both the Supervisor proxy
    and the kiosk, so ask for the entity. A missing entity is `None`, not an error:
    that is what produces the "no such entity" message rather than a scary failure.
    """
    now = time.time()
    with STATE_LOCK:
        hit = _ENTITY_CACHE.get(entity_id)
        if hit and now - hit[0] < STATE_TTL:
            return hit[1]
    if not SUPERVISOR_TOKEN:
        raise ValueError("Reading Home Assistant entities needs the homeassistant_api "
                         "permission and must run as a Home Assistant app.")
    request = Request(f"{SUPERVISOR_API}/states/{quote(entity_id)}",
                      headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                               "Content-Type": "application/json"})
    state = None
    try:
        with urlopen(request, timeout=15) as response:
            state = json.load(response)
    except HTTPError as error:
        if error.code != 404:
            raise ValueError(f"Home Assistant API returned HTTP {error.code}.") from None
    except (URLError, OSError):
        raise ValueError("Home Assistant API is unreachable.") from None
    with STATE_LOCK:
        _ENTITY_CACHE[entity_id] = (now, state)
        if len(_ENTITY_CACHE) > 64:                     # kiosks are long-lived
            for stale in sorted(_ENTITY_CACHE, key=lambda key: _ENTITY_CACHE[key][0])[:16]:
                _ENTITY_CACHE.pop(stale, None)
    return state


def home_point():
    """(latitude, longitude, source) for the kiosk's centre: the option, else zone.home."""
    global _ZONE_AT, _ZONE_POINT
    latitude = as_float(option("home_latitude"))
    longitude = as_float(option("home_longitude"))
    if latitude is not None and longitude is not None and (latitude or longitude):
        return latitude, longitude, "option"
    with STATE_LOCK:
        if _ZONE_POINT and time.time() - _ZONE_AT < ZONE_TTL:
            return _ZONE_POINT[0], _ZONE_POINT[1], "zone.home"
    latitude = longitude = None
    try:
        zone = entity_state("zone.home")
        attributes = (zone or {}).get("attributes") or {}
        latitude = as_float(attributes.get("latitude"))
        longitude = as_float(attributes.get("longitude"))
    except ValueError:
        pass
    if latitude is None or longitude is None:
        return None, None, "missing"
    with STATE_LOCK:
        _ZONE_POINT, _ZONE_AT = (latitude, longitude), time.time()
    return latitude, longitude, "zone.home"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
EARTH_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing in degrees clockwise from true north."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def sensor_bounds_km(bounds: str):
    """Half-diagonal of the sensor's tracked box, in km — the natural auto range.

    The Flightradar24 sensor publishes `bounds` as "lat1,lat2,lon1,lon2".
    """
    parts = _bounds_parts(bounds)
    if parts is None:
        return None
    lat1, lat2, lon1, lon2 = parts
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    return haversine_km(mid_lat, mid_lon, lat1, lon1)


def sensor_area_km(bounds: str):
    """Half-WIDTH of the sensor's box in km: how far each way the sensor looks.

    This is the number a user can act on ("my sensor only covers 50 km"), whereas the
    half-diagonal is the right thing for fitting the whole box on screen.
    """
    parts = _bounds_parts(bounds)
    if parts is None:
        return None
    lat1, lat2, lon1, lon2 = parts
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    north_south = haversine_km(lat1, mid_lon, lat2, mid_lon)
    east_west = haversine_km(mid_lat, lon1, mid_lat, lon2)
    return (north_south + east_west) / 4.0


def bounds_corners(bounds: str, latitude: float, longitude: float):
    """The sensor's box as four [bearing, km] corners, so the display can SHOW the
    area it is watching. Aircraft are only ever reported from inside it."""
    parts = _bounds_parts(bounds)
    if parts is None:
        return []
    lat1, lat2, lon1, lon2 = parts
    corners = []
    for corner_lat, corner_lon in ((lat1, lon1), (lat1, lon2), (lat2, lon2), (lat2, lon1)):
        corners.append([round(bearing_deg(latitude, longitude, corner_lat, corner_lon), 1),
                        round(haversine_km(latitude, longitude, corner_lat, corner_lon), 3)])
    return corners


def _bounds_parts(bounds: str):
    values = []
    for piece in str(bounds or "").split(","):
        value = as_float(piece)
        if value is None:
            return None
        values.append(value)
    return values if len(values) == 4 else None


# --------------------------------------------------------------------------- #
# Aircraft type -> schematic
#
# The single-aircraft dashboard draws a top-down silhouette of the aircraft type,
# not a generic plane. No free per-type image database exists — Flightradar24
# publishes airline logos and photos, neither of which covers "an A321 in side
# view" — so the artwork is a vendored set of 37 real silhouettes (see
# vendor/adsb-radar/README.md; icons by ADS-B Radar, https://adsb-radar.com) and
# the type designator the sensor already gives us (`aircraft_code`, an ICAO type
# like A320 / B38M / AW189) is matched to the closest one. Matching is therefore
# at FAMILY level: A321 and A20N both draw the A320 icon. Where nothing matches,
# the category decides, and a plain twin-jet airliner (`a5`) is the last resort.
#
# The names are the icon file stems in web/aircraft_icons.json, and the one that
# comes back is inlined into the page (never fetched) as row["icon"].
# --------------------------------------------------------------------------- #
ICON_BY_CODE = {}
for _icon, _codes in {
    # Airbus narrowbody / widebody / quad / superjumbo
    "a320": ("A318 A319 A320 A321 A19N A20N A21N".split()),
    "a330": "A300 A306 A30B A310 A332 A333 A337 A338 A339 A33F A358 A359 A35K A35X A3ST".split(),
    "a340": "A342 A343 A345 A346 A340".split(),
    "a380": "A380 A388".split(),
    # Boeing
    "b737": "B731 B732 B733 B734 B735 B736 B737 B738 B739 B73F B73G B37M B38M B39M B3XM B752 B753 B75F B75M".split(),
    "b747": "B741 B742 B743 B744 B748 B74D B74F B74S BLCF".split(),
    "b767": "B762 B763 B764 B767 B76F".split(),
    "b777": "B772 B773 B777 B778 B779 B77L B77W B77F".split(),
    "b787": "B787 B788 B789 B78X B781".split(),
    # McDonnell Douglas / legacy four-engine
    "md11": "MD11 DC10 MD10".split(),
    # Regional jets (the A220's shape is closest to an E-Jet)
    "crjx": "CRJ1 CRJ2 CRJ7 CRJ9 CRJX CL65".split(),
    "erj": "E135 E140 E145 E35L E45X E50P E55P".split(),
    "e195": "E170 E75L E75S E190 E195 E290 E295 BCS1 BCS3".split(),
    "f100": "F70 F100 F28".split(),
    # Turboprops (high straight wing, T-tail)
    "dh8a": ("DH8A DH8B DH8C DH8D DH8T DH3T DHC2 DHC3 DHC6 DHC7 DHP8 AT43 AT45 AT46 AT72 AT75 AT76 "
             "SF34 SB20 SH33 SH36 F27 F50 AN24 AN26 AN32 IL14 JS31 JS32 JS41 L410 B190").split(),
    # Military / outsize transports
    "c130": "C130 C30J L100 L382 AN12 AN72 AN74 AN124 AN225 A400 A40M IL76 C5M C17 C141 C160 CN35".split(),
    # Business jets
    "glf5": ("GLF2 GLF3 GLF4 GLF5 GLF6 G150 G200 G250 G280 G300 G350 G500 G550 G650 G700 GALX "
             "GA5C GA6C GA7C GA8C").split(),
    "fa7x": "FA10 FA20 FA50 FA7X FA8X F900 F2TH F5TH".split(),
    "learjet": "LJ23 LJ24 LJ25 LJ28 LJ31 LJ35 LJ36 LJ45 LJ55 LJ60 LJ75".split(),
    "a3": ("CL30 CL35 CL60 GL5T GL7T BD10 BD70 BD71 BD100 H25A H25B H25C H25X HA4T PC24 "
           "E545 E550 E135BJ E190BJ G200").split(),
    "a2": "C650 C680 C68A C700 C750 C25A C25B C25C C25M BE40 MU2".split(),
    "a0": "C500 C501 C510 C525 C550 C551 C560 C56X C550".split(),
    # Singles and light twins: a straight-wing light aircraft either way
    "cessna": ("C140 C150 C152 C172 C175 C177 C182 C185 C206 C207 C208 C210 C337 M20P M20T "
               "P28A P28B P28R P28T PA18 PA24 PA28 PA32 PA38 PA46 SR20 SR22 S22T DA40 DA62 "
               "BE19 BE33 BE35 BE36 DR40 PC12 TBM7 TBM8 TBM9 TBM85 GA8 E300".split()),
    "a1": ("BE10 BE20 BE30 BE9L BE9T BE99 B100 B200 B300 B350 C310 C340 C401 C402 C404 C414 "
           "C421 C425 C441 AC50 AC68 AC90 AC95 AEST ASTR PAY2 PAY3 PAY4 PA31 PA34 PA27 "
           "SW3 SW4 C90 C90A E110 E120").split(),
    # Rotary and the rest of the sky
    "a7": ("A109 A119 A139 A169 A189 AS32 AS35 AS50 AS55 AS65 AS3B AW139 AW189 BK11 BK17 B06 "
           "B105 B212 B222 B230 B407 B412 B427 B429 B430 B505 BH06 EC20 EC25 EC30 EC35 EC45 "
           "EC55 EC75 EXPL H47 H53 H60 H125 H135 H145 H160 H175 H215 KA32 LYNX MD52 MI8 MI17 "
           "MI24 NH90 PUMA R22 R44 R66 S55 S76 S92 UH1 EC35").split(),
    "a6": "CONC TU144 F16 F18 F15 F22 F35 MIG29 SU27 EF2000 GR4".split(),
    "b1": "AS25 AS26 ASK2 DG80 DG10 DUOD GLID L13 NIMB".split(),
    "b4": "MT03 CAV QUIC TRIK".split(),
}.items():
    for _code in _codes:
        ICON_BY_CODE.setdefault(_code, _icon)

# mnemonic icons for the non-fixed-wing categories the sensor may report
ICON_BY_CATEGORY = {
    "helicopter": "a7", "rotorcraft": "a7", "gyrocopter": "f15", "autogyro": "f15",
    "glider": "b1", "sailplane": "b1", "hang glider": "b4", "hangglider": "b4",
    "paraglider": "f5", "paramotor": "f5", "balloon": "b2", "airship": "b2",
    "lighter-than-air": "b2", "drone": "b0", "uav": "b0", "multirotor": "c0",
    "ultralight": "b4",
}

# Model-text fallback, for the day the sensor reports a code this table has never
# seen. Ordered: the first pattern that matches wins.
ICON_BY_MODEL = [
    # Specific families before the loose number rules below, or "777" would be
    # swallowed by the generic 7[0-9]{2} pattern first.
    (r"\bA38[08]\b", "a380"),
    (r"\bA34[0-9]\b", "a340"),
    (r"\bA35[0-9X]\b", "a330"),
    (r"\bA33[0-9]\b", "a330"),
    (r"\bA31[0-9]\b", "a330"),
    (r"\bA30[0-9]\b", "a330"),
    (r"\bA32[01]\b", "a320"),
    (r"\bA319\b", "a320"),
    (r"\bA22[0-9]\b", "e195"),
    (r"\b747\b", "b747"),
    (r"\b767\b", "b767"),
    (r"\b777\b", "b777"),
    (r"\b787\b", "b787"),
    (r"\b737\b", "b737"),
    (r"\b7[0-9]{2}\b", "b737"),
    (r"helicopter|rotorcraft|eurocopter|airbus helicopter|robinson", "a7"),
    (r"gulfstream|global \d", "glf5"),
    (r"falcon", "fa7x"),
    (r"learjet", "learjet"),
    (r"citation|phenom|hawker|challenger", "a2"),
    (r"boeing 7|airbus a", "a5"),
    (r"dash 8|atr \d|twin otter|saab|king air|turboprop", "dh8a"),
    (r"cessna|piper|cirrus|diamond|pilatus|tbm|socata|eclipse", "cessna"),
    (r"embraer|erj|legacy", "erj"),
    (r"md-?1[01]|dc-?10", "md11"),
    (r"glider|sailplane", "b1"),
    (r"drone|uav|quadcopter", "b0"),
]


def aircraft_icon(code: str, model: str, category: str) -> str:
    """Icon name for one aircraft, or "" when there is nothing sensible to draw.

    Order matters: the exact type designator is the most reliable signal, the
    category next (a helicopter is never an airliner even if its code is odd),
    then the model's words, then a generic twin-jet airliner.
    """
    code = (code or "").strip().upper()
    model = (model or "").strip()
    category = (category or "").strip().lower()
    if code in ("GRND", "GND", "GROUND") or "ground vehicle" in category:
        # Airport vehicles are sometimes listed as "aircraft" with a position; a
        # schematic for them would be a lie, so the display falls back to the
        # plain plane glyph it already has.
        return ""
    if code in ICON_BY_CODE:
        return ICON_BY_CODE[code]
    for key, icon in ICON_BY_CATEGORY.items():
        if key in category:
            return icon
    for pattern, icon in ICON_BY_MODEL:
        if re.search(pattern, model, re.I):
            return icon
    return "a5"


# --------------------------------------------------------------------------- #
# Top-down -> side view
#
# The side profiles are a smaller set than the plan views, because a profile
# distinguishes aircraft by class — engine count, wing position, propellers, tail
# — and not by family. So this maps the already-resolved plan-view icon onto the
# profile that matches it: a 737 and an A320 share one, a 777 and a 787 share a
# heavier one, a 747's four engines get their own. Anything with no profile (a
# balloon, a drone, a ground vehicle) keeps its plan view rather than being given
# a wrong profile.
# --------------------------------------------------------------------------- #
SIDE_BY_ICON = {
    "a320": "side-jet", "b737": "side-jet", "a5": "side-jet", "a4": "side-jet",
    "a6": "side-jet",
    "a330": "side-heavy", "b767": "side-heavy", "b777": "side-heavy",
    "b787": "side-heavy", "md11": "side-heavy",
    "a340": "side-quad", "a380": "side-quad", "b747": "side-quad",
    "crjx": "side-regional", "erj": "side-regional", "e195": "side-regional",
    "f100": "side-regional",
    "a0": "side-bizjet", "a2": "side-bizjet", "a3": "side-bizjet",
    "fa7x": "side-bizjet", "glf5": "side-bizjet", "learjet": "side-bizjet",
    "a1": "side-turboprop", "dh8a": "side-turboprop", "c130": "side-turboprop",
    "cessna": "side-light",
    "b1": "side-glider",
    "a7": "side-heli",
}


def side_view_icon(icon: str) -> str:
    """The side profile for a plan-view icon, or the icon itself if none fits."""
    return SIDE_BY_ICON.get(icon, icon)


# --------------------------------------------------------------------------- #
# Airline logos
#
# The dashboard can show the airline's actual logo instead of a code badge. The
# artwork comes from the same place as the data — Flightradar24's operator logo
# set, keyed by the airline's ICAO code (`EIN`, not `EI`, which has no file) — and
# the same three rules apply as everywhere else in this app:
#
#   * the KIOSK never makes the request and needs no internet: the app fetches on
#     the machine that runs it, once per airline, and hands the page a `data:` URI
#     (so it also renders in the admin's srcdoc preview, and keeps working after
#     the fetch could no longer succeed);
#   * the app never BLOCKS a display on that fetch: a poll returns whatever is
#     cached and asks a background thread for the rest, so a slow or dead CDN
#     costs a code badge for a few seconds, never a stalled display;
#   * failures are remembered — a miss is not retried for an hour, and a run of
#     network failures stands the whole thing down for half an hour — so a Home
#     Assistant with no internet does not spend every poll on timeouts.
#
# Only airlines this display actually shows are ever fetched (never the sensor's
# whole area), and the cache lives in /data next to the display store, so it
# survives a restart and the artwork is downloaded once, ever.
#
# Trademark note: these are the airlines' own marks as published by Flightradar24
# alongside its data. This is a personal, non-commercial display that credits
# Flightradar24 in its footer; "Airline logo: code badge" turns it off entirely,
# per display, and a logo of your own replaces any single airline's.
# --------------------------------------------------------------------------- #
LOGO_BASE_URL = "https://www.flightradar24.com/static/images/data/operators/{icao}_logo0.png"
LOGO_USER_AGENT = "Mozilla/5.0 (compatible; KioskFlightDisplays/1.0)"
LOGO_TIMEOUT = 10.0
LOGO_MAX_BYTES = 400_000
LOGO_RETRY_AFTER = 3600.0         # one failure: leave that airline alone for an hour
LOGO_OFFLINE_AFTER = 1800.0       # no internet: stand down for half an hour
LOGO_FAILURES_BEFORE_OFFLINE = 3
LOGO_MAX_PARALLEL = 4

# --------------------------------------------------------------------------- #
# One fetcher, two kinds of artwork
#
# Airline logos and aircraft images obey exactly the same rules, so they share
# one cache implementation. The rules, each of which cost a bug to learn:
#
#   * get() NEVER waits for the network. It returns what is on disk or in memory
#     and asks a worker thread for anything missing, so a slow or dead upstream
#     costs a fallback (a code badge, a silhouette) for a few seconds and never a
#     stalled display;
#   * a miss is remembered for `retry_after`, and after `failures_before_offline`
#     network errors the whole cache stands down for `offline_after` with ONE log
#     line — a Home Assistant with no internet must not spend every poll on
#     timeouts;
#   * a 4xx is "this key has no artwork", NOT "the internet is down";
#   * what comes back is validated (`accepts`), so an error page or a truncated
#     download is never cached as art;
#   * the cache lives under /data, survives restarts, and is never in the repo.
# --------------------------------------------------------------------------- #
def guess_mime(data: bytes) -> str:
    """The MIME type of some artwork, from its magic bytes ("" = not artwork)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] in (b"<?xml", b"<svg ") or data[:4] == b"<svg":
        return "image/svg+xml"
    return ""


EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/svg+xml": ".svg"}


class AssetCache:
    """One fetched asset per key: memory -> disk -> background fetch."""

    def __init__(self, name, directory, url_for, headers, accepts, *,
                 timeout=10.0, max_bytes=400_000, retry_after=3600.0,
                 offline_after=1800.0, failures_before_offline=3, max_parallel=4,
                 key_pattern=r"[A-Z0-9](?:[A-Z0-9_]{0,14})", hint=""):
        self.name = name
        self.directory = directory          # callable: settings can change while running
        self.url_for = url_for              # callable(key) -> url, or "" for library-only
        self.headers = headers              # callable() -> dict
        self.accepts = accepts
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retry_after = retry_after
        self.offline_after = offline_after
        self.failures_before_offline = failures_before_offline
        self.key_pattern = key_pattern
        self.hint = hint
        self.lock = threading.Lock()
        self.memory: dict = {}
        self.offline_until = 0.0
        self.failures = 0
        self.slots = threading.Semaphore(max_parallel)
        self._index_at = 0.0
        self._index: dict = {}

    # -- reading ----------------------------------------------------------- #
    def _index_names(self) -> dict:
        """Optional `index.json` in the directory: {"EIN_A21N": "some file.jpg"}.

        It exists so a library can be populated with arbitrary filenames (a
        personal collection, or a export from a stock purchase) without renaming
        everything to the app's convention.
        """
        now = time.time()
        if now - self._index_at < 5.0:
            return self._index
        self._index_at = now
        try:
            value = json.loads((self.directory() / "index.json").read_text())
            self._index = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._index = {}
        return self._index

    def _read(self, key: str):
        directory = self.directory()
        for extension in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            try:
                data = (directory / f"{key}{extension}").read_bytes()
            except OSError:
                continue
            if data and self.accepts(data):
                return data
        name = self._index_names().get(key)
        if name:
            try:
                data = (directory / str(name)).read_bytes()
            except OSError:
                return None
            if data and self.accepts(data):
                return data
        return None

    def get(self, key: str):
        key = (key or "").strip().upper()
        if not re.fullmatch(self.key_pattern, key):
            return None
        now = time.time()
        with self.lock:
            state = self.memory.get(key)
            if state:
                if state["data"]:
                    return state["data"]
                if state["busy"] or now - state["at"] < self.retry_after:
                    return None
            if self.offline_until > now:
                return None
        data = self._read(key)
        if data:
            with self.lock:
                self.memory[key] = {"data": data, "at": time.time(), "busy": False}
            return data
        if self.url_for(key):
            self._start(key)
        return None

    # -- fetching ---------------------------------------------------------- #
    def _start(self, key: str) -> None:
        with self.lock:
            state = self.memory.setdefault(key, {"data": None, "at": 0.0, "busy": False})
            if state["busy"]:
                return
            state["busy"] = True
        threading.Thread(target=self._run, args=(key,), daemon=True).start()

    def _run(self, key: str) -> None:
        url = self.url_for(key)
        data = None
        network_error = False
        with self.slots:
            if url:
                try:
                    request = Request(url, headers=self.headers())
                    with urlopen(request, timeout=self.timeout) as response:
                        if int(getattr(response, "status", 0) or 0) == 200:
                            data = response.read(self.max_bytes + 1)
                except HTTPError as error:
                    network_error = error.code >= 500
                except (URLError, OSError, ValueError):
                    network_error = True
        if data is not None and (len(data) > self.max_bytes or not self.accepts(data)):
            data = None
            network_error = True
        now = time.time()
        with self.lock:
            self.memory[key] = {"data": data, "at": now, "busy": False}
            if data:
                self.failures = 0
            elif network_error:
                self.failures += 1
                if self.failures >= self.failures_before_offline and self.offline_until <= now:
                    self.offline_until = now + self.offline_after
                    print(f"kiosk-flight: {self.name} unreachable; not retrying for "
                          f"{int(self.offline_after / 60)} minutes{self.hint}", flush=True)
        if not data:
            return
        try:
            directory = self.directory()
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f"{key}.tmp"
            temporary.write_bytes(data)
            temporary.replace(directory / f"{key}{EXT_BY_MIME.get(guess_mime(data), '.bin')}")
        except OSError:
            pass                       # a read-only /data costs a re-fetch, never a failure


def is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def is_artwork(data: bytes) -> bool:
    return bool(guess_mime(data))


# Airline logos the app fetches for you: Flightradar24's operator set, keyed on
# the ICAO code, kept in a `.fetched` sub-folder of the logo folder so that the
# folder itself is only ever the user's own artwork.
LOGO_CACHE = AssetCache(
    "airline logos",
    directory=lambda: logo_directory() / ".fetched",
    url_for=lambda key: LOGO_BASE_URL.format(icao=key),
    headers=lambda: {"User-Agent": LOGO_USER_AGENT},
    accepts=is_png, timeout=LOGO_TIMEOUT, max_bytes=LOGO_MAX_BYTES,
    retry_after=LOGO_RETRY_AFTER, offline_after=LOGO_OFFLINE_AFTER,
    failures_before_offline=LOGO_FAILURES_BEFORE_OFFLINE,
    max_parallel=LOGO_MAX_PARALLEL, key_pattern=r"[A-Z0-9]{2,4}",
    hint=" (the airline's code is shown)")


def logo_enabled(config: dict) -> bool:
    return str(config.get("airlineLogo", DEFAULTS["airlineLogo"])) != "monogram"


# --------------------------------------------------------------------------- #
# Your own logos
#
# A folder of image files, each answering for the airline whose name it carries
# ("Aer Lingus.png") or whose code it carries ("EIN.png") — no renaming to the
# app's conventions, no per-airline setting. Matching reduces both sides to
# letters and digits (logo_slug), so case, spaces, dots and the extension are
# all free, and an airline whose published mark is wrong, dated or missing gets
# replaced by dropping a file in:
#
#   * a file in the folder beats anything the app would have fetched, and beats
#     the code badge an airline with no published mark would otherwise show;
#   * `index.json` still covers what a filename cannot say twice —
#     {"EIN": "aer-lingus-2019.png"} — and the app writes one for logos added
#     through the admin page;
#   * the app's own fetched artwork lives in a `.fetched` sub-folder, so nothing
#     in this index is ever anything but the user's;
#   * the index is re-read every few seconds, so a file dropped in over Samba or
#     through the File editor shows up without restarting the app.
# --------------------------------------------------------------------------- #
LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".svg")
LOGO_INDEX_SECONDS = 5.0
LOGO_UPLOAD_MAX = LOGO_MAX_BYTES      # the same ceiling the fetcher enforces
_logo_index = {"at": 0.0, "files": {}}


def logo_directory() -> Path:
    """The folder the user's own logos are read from."""
    configured = as_text(option("logo_library"), 200)
    return Path(configured) if configured else DATA_FILE.parent / "logos"


def logo_slug(value) -> str:
    """A name or code reduced to what two spellings of an airline agree on."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def logo_library() -> dict:
    """{slug: Path} for the files in the logo folder, re-read every few seconds."""
    now = time.time()
    if now - _logo_index["at"] < LOGO_INDEX_SECONDS:
        return _logo_index["files"]
    found = {}
    try:
        # Same order as logo_files(): where two names reduce to one key, the last
        # one wins, and the panel must report the same winner the lookup uses.
        entries = sorted(logo_directory().iterdir(), key=lambda path: path.name.lower())
    except OSError:
        entries = []                  # no folder yet: the fetcher will make one
    for path in entries:
        if path.is_file() and path.suffix.lower() in LOGO_EXTENSIONS:
            found[logo_slug(path.stem)] = path
    try:
        named = json.loads((logo_directory() / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        named = {}
    if isinstance(named, dict):
        for key, name in named.items():
            path = logo_directory() / str(name)
            if path.is_file() and path.suffix.lower() in LOGO_EXTENSIONS:
                found.setdefault(logo_slug(key), path)
    _logo_index["at"] = now
    _logo_index["files"] = found
    return found


def logo_names(row: dict) -> list:
    """Every key one airline could be filed under, best first.

    The codes are exact matches; the name is whatever the sensor calls it, plus
    its first word, so a file called "Ryanair.png" answers for the row the sensor
    names "Ryanair Holdings" and vice versa (see the prefix rule in logo_file).
    """
    keys = []
    for value in (row.get("airlineIcao"), row.get("airlineCode"), row.get("airline")):
        slug = logo_slug(value)
        if slug and slug not in keys:
            keys.append(slug)
    first = logo_slug(str(row.get("airline") or "").split(" ")[0])
    if len(first) >= 4 and first not in keys:
        keys.append(first)
    return keys


def logo_file(row: dict):
    """The user's own file for an airline, or None to fall through to a fetch."""
    library = logo_library()
    keys = logo_names(row)
    for key in keys:                  # exact first: a code, or the name itself
        if key in library:
            return library[key]
    for key in keys:                  # then a name that starts the filename's
        if len(key) < 4:              # never a 2-3 letter code, or EIN.png would
            continue                  # answer for every airline starting "E…"
        for slug, path in library.items():
            if slug.startswith(key) or (len(slug) >= 4 and key.startswith(slug)):
                return path
    return None


def logo_bytes(row: dict):
    """Artwork for one airline: the user's file, else the app's fetched copy.

    None until a fetch has finished — which is what leaves the badge showing the
    airline's code for the first poll or two of an airline it has never seen.
    """
    path = logo_file(row)
    if path:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if is_artwork(data) else None
    data = LOGO_CACHE.get(as_text(row.get("airlineIcao"), 4))
    return logo_art(data) if data else None


def logo_data_uri(row: dict) -> str:
    data = logo_bytes(row)
    if not data:
        return ""
    mime = guess_mime(data) or "image/png"
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- #
# The plate behind a logo
#
# A handful of airlines publish their mark as artwork that carries its own
# backing: Thomson/TUI and Jetairfly as a pale blue-violet box, Norwegian as a
# red one. On the dashboard's white badge that box reads as a purple rectangle
# around a logo that has no purple in it, so a fetched plate is repainted white:
#
#   * transparency is left alone — it already lets the white badge through;
#   * a plate the mark NEEDS is left alone: Norwegian's wordmark is white, and
#     white on white is nothing, so artwork whose ink sits lighter than its plate
#     keeps both;
#   * only PNG is touched (everything the fetcher accepts is), and only with
#     zlib and the PNG spec — no image library on a box that may be an i386.
#     Anything this reader cannot decode is passed through unchanged, so the one
#     failure mode is "the plate stays".
#   * your own files are never touched: art supplied by hand is shown as supplied.
# --------------------------------------------------------------------------- #
PLATE_TOLERANCE = 30          # per-channel distance that still reads as the plate
PLATE_BORDER_SHARE = 0.6      # border pixels that must be plate to call it one
PLATE_WHITE_FROM = 245        # a plate this light is already the badge's colour
PLATE_LIGHT_INK = 0.62        # a mark this light cannot show against white
PLATE_LIGHT_SHARE = 0.05      # ... and this much of it means the plate is wanted
_plate_cache: dict = {}


def _png_rgba(data: bytes):
    """(width, height, RGBA8 bytes) for a plain 8-bit PNG, or None.

    Deliberately not a PNG implementation: no interlacing, no sub-byte samples,
    no 16-bit — all of which the artwork this app reads has, and none of which is
    worth the code on a device that only ever needs to look at 150×40 pixels.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, header, idat, palette, transparency = 8, None, bytearray(), None, None
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            return None                       # truncated chunk: not ours to guess
        if kind == b"IHDR":
            header = body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not header or len(header) < 13:
        return None
    width = int.from_bytes(header[0:4], "big")
    height = int.from_bytes(header[4:8], "big")
    depth, colour, method, filtering, interlace = header[8], header[9], header[10], header[11], header[12]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None or depth != 8 or interlace or method or filtering:
        return None
    if width < 1 or height < 1 or width * height > 4_000_000:
        return None
    if colour == 3 and (not palette or len(palette) % 3):
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    stride = width * channels
    if len(raw) != (stride + 1) * height:
        return None
    out = bytearray(width * height * 4)
    previous = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = previous[i]
                corner = previous[i - channels] if i >= channels else 0
                guess = left + up - corner
                da, db, dc = abs(guess - left), abs(guess - up), abs(guess - corner)
                nearest = left if (da <= db and da <= dc) else (up if db <= dc else corner)
                line[i] = (line[i] + nearest) & 0xFF
        elif kind != 0:
            return None
        previous = bytes(line)
        at = y * width * 4
        if colour == 6:
            out[at:at + stride] = line
        elif colour == 2:
            for x in range(width):
                out[at + x * 4:at + x * 4 + 3] = line[x * 3:x * 3 + 3]
                out[at + x * 4 + 3] = 255
        elif colour == 0:
            for x in range(width):
                grey = line[x]
                out[at + x * 4:at + x * 4 + 3] = bytes([grey, grey, grey])
                out[at + x * 4 + 3] = 255
        elif colour == 4:
            for x in range(width):
                grey = line[x * 2]
                out[at + x * 4:at + x * 4 + 3] = bytes([grey, grey, grey])
                out[at + x * 4 + 3] = line[x * 2 + 1]
        else:
            table, alpha = palette or b"", transparency or b""
            for x in range(width):
                index = line[x]
                if (index + 1) * 3 > len(table):
                    continue                              # index outside the palette
                out[at + x * 4:at + x * 4 + 3] = table[index * 3:index * 3 + 3]
                out[at + x * 4 + 3] = alpha[index] if index < len(alpha) else 255
    return width, height, out


def _png_write(width: int, height: int, rgba: bytes) -> bytes:
    """A PNG (8-bit RGBA, filter 0) — the plate change is written back as-is."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgba[y * width * 4:(y + 1) * width * 4]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (len(body).to_bytes(4, "big") + kind + body
                + zlib.crc32(kind + body).to_bytes(4, "big"))

    head = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def white_plate_png(data: bytes):
    """`data` with a solid backing plate repainted white, or None if left alone."""
    decoded = _png_rgba(data)
    if not decoded:
        return None
    width, height, rgba = decoded
    border = ([(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
              + [(0, y) for y in range(height)] + [(width - 1, y) for y in range(height)])
    tally: dict = {}
    opaque = 0
    for x, y in border:
        at = (y * width + x) * 4
        pixel = bytes(rgba[at:at + 4])
        if pixel[3] < 200:
            continue
        opaque += 1
        tally[pixel] = tally.get(pixel, 0) + 1
    if opaque < 0.9 * len(border):
        return None                   # transparent artwork: the badge shows through
    plate = max(tally, key=lambda pixel: tally[pixel])
    # Nearly, not exactly, the plate: a rounded corner or a one-pixel jpeg-style
    # edge gives the same backing a dozen neighbouring values.
    same = 0
    for x, y in border:
        at = (y * width + x) * 4
        if max(abs(rgba[at + i] - plate[i]) for i in range(3)) <= PLATE_TOLERANCE:
            same += 1
    if same < PLATE_BORDER_SHARE * len(border):
        return None                   # a busy edge is artwork, not a backing
    if min(plate[:3]) >= PLATE_WHITE_FROM:
        return None                   # already the colour of the badge
    ink, light = 0, 0
    for at in range(0, width * height * 4, 4):
        pixel = rgba[at:at + 4]
        if max(abs(pixel[i] - plate[i]) for i in range(3)) <= PLATE_TOLERANCE:
            rgba[at:at + 4] = b"\xff\xff\xff\xff"
            continue
        ink += 1
        if (0.299 * pixel[0] + 0.587 * pixel[1] + 0.114 * pixel[2]) / 255.0 > PLATE_LIGHT_INK:
            light += 1
    if not ink:
        return None
    # A mark this light over more than a sliver of itself cannot survive a white
    # badge: Norwegian's wordmark is white and Jetairfly's "fly" is pale blue, and
    # both are legible only because of the plate behind them. Antialiased edges
    # against the plate are light too, but they are a fringe, not a fifth of the
    # mark. Checked as a share of the whole image so a thin sublogo (a 2-pixel
    # "fly" in 2800 pixels) still counts.
    if light > PLATE_LIGHT_SHARE * width * height:
        return None
    return _png_write(width, height, bytes(rgba))


def logo_art(data: bytes) -> bytes:
    """white_plate_png() behind a small memory cache: the artwork is fetched once,
    but every poll rebuilds the payload it goes into."""
    key = hashlib.sha1(data).hexdigest()
    if key not in _plate_cache:
        if len(_plate_cache) > 64:
            _plate_cache.clear()
        _plate_cache[key] = white_plate_png(data) or data
    return _plate_cache[key]



# --------------------------------------------------------------------------- #
# Aircraft images (per airline + type)
#
# The dashboard can show a picture of the actual airliner — an airline's livery
# on the type the sensor reports — instead of a drawn silhouette. Two sources,
# in this order, both private to the machine that runs the app:
#
#   1. a keyed API, configured in the app options as a URL template
#      (image_api_url with {icao}, {type} and {key} placeholders) — the shape
#      most aviation-asset APIs use, e.g. logostream's livery endpoint;
#   2. a local image library directory (image_library, default /data/liveries):
#      <ICAO>_<TYPE>.jpg or a file named by index.json — for artwork the user has
#      licensed or collected themselves.
#
# Anything else falls back to the vendored silhouettes, so a display never shows
# nothing. **No image bytes are ever stored in the repository**: this is the
# user's own material, kept under /data, exactly like the logo cache.
# --------------------------------------------------------------------------- #
IMAGE_TIMEOUT = 15.0
IMAGE_MAX_BYTES = 900_000
IMAGE_RETRY_AFTER = 3600.0
IMAGE_OFFLINE_AFTER = 1800.0
IMAGE_FAILURES_BEFORE_OFFLINE = 3
IMAGE_MAX_PARALLEL = 3
IMAGE_USER_AGENT = LOGO_USER_AGENT


def image_enabled(config: dict) -> bool:
    return str(config.get("typeImage", DEFAULTS["typeImage"])) == "image"


def image_directory() -> Path:
    configured = as_text(option("image_library"), 200)
    return Path(configured) if configured else DATA_FILE.parent / "liveries"


def image_url(key: str) -> str:
    template = as_text(option("image_api_url"), 400)
    if not template:
        return ""
    icao, _, code = key.partition("_")
    try:
        return template.format(icao=icao, type=code, key=as_text(option("image_api_key"), 200))
    except (KeyError, IndexError, ValueError):
        return template


def image_headers() -> dict:
    api_key = as_text(option("image_api_key"), 200)
    headers = {"User-Agent": IMAGE_USER_AGENT, "Accept": "image/*"}
    if api_key:
        # Aviation asset APIs differ on the header name; send both rather than
        # make the user care (logostream documents x-api-key).
        headers["x-api-key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


IMAGE_CACHE = AssetCache(
    "aircraft images",
    directory=image_directory,
    url_for=image_url,
    headers=image_headers,
    accepts=is_artwork, timeout=IMAGE_TIMEOUT, max_bytes=IMAGE_MAX_BYTES,
    retry_after=IMAGE_RETRY_AFTER, offline_after=IMAGE_OFFLINE_AFTER,
    failures_before_offline=IMAGE_FAILURES_BEFORE_OFFLINE,
    max_parallel=IMAGE_MAX_PARALLEL, key_pattern=r"[A-Z0-9]{2,4}_[A-Z0-9]{2,6}",
    hint=" (the drawn silhouette is shown)")


def aircraft_image_key(row: dict) -> str:
    icao = str(row.get("airlineIcao") or "").strip().upper()
    code = str(row.get("code") or "").strip().upper()
    if not icao or not code or code == "GRND":
        return ""
    return f"{icao}_{code}"


def aircraft_image_uri(row: dict) -> str:
    key = aircraft_image_key(row)
    if not key:
        return ""
    data = IMAGE_CACHE.get(key)
    if not data:
        return ""
    return f"data:{guess_mime(data)};base64," + base64.b64encode(data).decode("ascii")


def aircraft_images(flights: list, config: dict) -> dict:
    """Data URIs for the aircraft pictures a payload shows, keyed `<ICAO>_<TYPE>`.

    Deduplicated like the logos: four Ryanair 737s must not carry the same JPEG
    four times over the LAN.
    """
    if not image_enabled(config):
        return {}
    images = {}
    seen = set()
    for row in flights:
        key = aircraft_image_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        uri = aircraft_image_uri(row)
        if uri:
            images[key] = uri
    return images


def airline_logos(flights: list, config: dict) -> dict:
    """Data URIs for the airlines a payload shows, keyed by the airline's own key.

    The key is the ICAO code, or — for a flight the sensor has no code for — the
    slug of its name; the page's logoKey() builds the same key, so a logo can be
    answered by a file named after the airline alone.

    Deduplicated and sent once per airline rather than once per aircraft: a
    dozen arrivals from one airline should not carry the same 8 KB of PNG twelve
    times over the LAN.
    """
    if not logo_enabled(config):
        return {}
    logos = {}
    tried = set()
    for row in flights:
        key = as_text(row.get("airlineIcao"), 4).upper() or logo_slug(row.get("airline"))
        if not key or key in tried:
            continue
        tried.add(key)
        uri = logo_data_uri(row)
        if uri:
            logos[key] = uri
    return logos


# --------------------------------------------------------------------------- #
# Flight payload
# --------------------------------------------------------------------------- #
def flight_rows(config: dict) -> dict:
    """The JSON the display renders: aircraft as polar coordinates from the centre.

    Working in (bearing, distance) server-side keeps the kiosk page free of
    trigonometry and of any assumption about where north is on screen.
    """
    entity_id = as_text(config.get("entityId"), 120)
    payload = {
        "generatedAt": time.time(),
        "entityId": entity_id,
        "entityName": "",
        "entityState": None,
        "sensorBounds": "",
        "areaKm": None,
        "areaBox": [],
        "areaCount": 0,
        "hiddenGround": 0,
        "rangeKm": None,
        "viewKm": None,
        "rings": [],
        "total": 0,
        "flights": [],
        "logos": {},
        "images": {},
        "error": "",
        "warning": "",
        "centre": None,
    }
    if config.get("centreMode") == "custom":
        latitude = as_float(config.get("latitude"))
        longitude = as_float(config.get("longitude"))
        source = "custom"
    else:
        latitude = longitude = None
        source = "home"
    if latitude is None or longitude is None:
        latitude, longitude, source = home_point()
    if latitude is None or longitude is None:
        payload["error"] = ("No centre point: set a home location on the map in Home Assistant "
                            "(zone.home) or give this display custom coordinates.")
        return payload
    payload["centre"] = {"latitude": latitude, "longitude": longitude, "source": source}

    if not entity_id:
        payload["error"] = "No sensor chosen for this display."
        return payload
    try:
        state = entity_state(entity_id)
    except ValueError as error:
        payload["error"] = str(error)
        return payload
    if state is None:
        payload["error"] = (f"Home Assistant has no entity {entity_id}. Install and configure the "
                            "Flightradar24 integration, or pick another sensor.")
        return payload
    attributes = state.get("attributes") or {}
    flights = attributes.get("flights")
    if not isinstance(flights, list):
        payload["error"] = (f"{entity_id} has no 'flights' list. It reports "
                            f"'{as_text(state.get('state'), 40)}' — pick a Flightradar24 "
                            "'in area' sensor.")
        return payload
    payload["entityState"] = as_text(state.get("state"), 40)
    payload["entityName"] = as_text(attributes.get("friendly_name") or entity_id, 80)
    payload["sensorBounds"] = as_text(attributes.get("bounds"), 80)
    box_km = sensor_bounds_km(payload["sensorBounds"])
    area_km = sensor_area_km(payload["sensorBounds"])
    payload["areaKm"] = round(area_km, 1) if area_km else None
    payload["areaBox"] = bounds_corners(payload["sensorBounds"], latitude, longitude)
    hide_on_ground = truthy(config.get("hideOnGround"))

    rows = []
    hidden_ground = 0
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        if truthy(flight.get("on_ground"), False):
            hidden_ground += 1
            if hide_on_ground:
                continue
        fl_lat = as_float(flight.get("latitude"))
        fl_lon = as_float(flight.get("longitude"))
        if fl_lat is None or fl_lon is None:
            continue
        distance = haversine_km(latitude, longitude, fl_lat, fl_lon)
        vertical = as_float(flight.get("vertical_speed"))
        trend = "level"
        if vertical is not None:
            if vertical >= 300:
                trend = "climb"
            elif vertical <= -300:
                trend = "descent"
        row = {
            "callsign": as_text(flight.get("callsign"), 20),
            "flight": as_text(flight.get("flight_number"), 20),
            "registration": as_text(flight.get("aircraft_registration"), 20),
            "airline": as_text(flight.get("airline_short") or flight.get("airline"), 40),
            # The code badge in the single-aircraft view is the airline's own
            # code — IATA when the sensor has it (two letters, what is painted on
            # the tail), else ICAO. It is a code, not a name, so it is kept apart
            # from `airline`.
            "airlineCode": as_text(flight.get("airline_iata") or flight.get("airline_icao"), 4),
            # The logo is keyed on ICAO, not IATA: Flightradar24's operator set has
            # EIN_logo0.png but no EI_logo0.png, so the badge's two-letter code and
            # the logo's lookup key are different fields on purpose.
            "airlineIcao": as_text(flight.get("airline_icao"), 4),
            "type": as_text(flight.get("aircraft_model") or flight.get("aircraft_code"), 40),
            "code": as_text(flight.get("aircraft_code"), 12),
            "model": as_text(flight.get("aircraft_model"), 40),
            "category": as_text(flight.get("aircraft_category"), 20),
            "originCode": as_text(flight.get("airport_origin_code_iata")
                                  or flight.get("airport_origin_code_icao"), 8),
            "originCity": as_text(flight.get("airport_origin_city")
                                  or flight.get("airport_origin_name"), 40),
            "destinationCode": as_text(flight.get("airport_destination_code_iata")
                                       or flight.get("airport_destination_code_icao"), 8),
            "destinationCity": as_text(flight.get("airport_destination_city")
                                       or flight.get("airport_destination_name"), 40),
            "altitude": as_float(flight.get("altitude")),
            "speed": as_float(flight.get("ground_speed")),
            "heading": as_float(flight.get("heading")),
            "vertical": vertical,
            "trend": trend,
            "onGround": truthy(flight.get("on_ground"), False),
            "distanceKm": round(distance, 3),
            "bearing": round(bearing_deg(latitude, longitude, fl_lat, fl_lon), 1),
        }
        row["name"] = (row["callsign"] or row["flight"] or row["registration"]
                       or "Unknown flight")
        row["icon"] = aircraft_icon(row["code"], row["model"], row["category"])
        # Which set the display asked for. Both names travel nowhere else: the
        # page just looks up `icon` in the map it was given.
        if str(config.get("typeGraphic", DEFAULTS["typeGraphic"])) == "side":
            row["icon"] = side_view_icon(row["icon"])
        trail = []
        for point in (flight.get("coordinates") or [])[-40:]:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            point_lat, point_lon = as_float(point[0]), as_float(point[1])
            if point_lat is None or point_lon is None:
                continue
            trail.append([round(bearing_deg(latitude, longitude, point_lat, point_lon), 1),
                          round(haversine_km(latitude, longitude, point_lat, point_lon), 3)])
        row["trail"] = trail
        rows.append(row)

    total = len(rows)
    sort_by = config.get("sortBy") if config.get("sortBy") in SORT_KEYS else "nearest"
    if sort_by == "nearest":
        rows.sort(key=lambda r: r["distanceKm"])
    elif sort_by == "lowest":
        rows.sort(key=lambda r: (r["altitude"] is None, r["altitude"] or 0))
    elif sort_by == "highest":
        rows.sort(key=lambda r: -(r["altitude"] or 0))
    elif sort_by == "fastest":
        rows.sort(key=lambda r: -(r["speed"] or 0))
    else:
        rows.sort(key=lambda r: r["name"].lower())

    try:
        limit = int(float(str(config.get("maxFlights", "6"))))
    except (TypeError, ValueError):
        limit = 6
    limit = max(1, min(20, limit))
    payload["total"] = total
    payload["hiddenGround"] = hidden_ground
    # What the sensor itself reports for its area — the number that answers "why is
    # my display empty when Flightradar shows aircraft?".
    payload["areaCount"] = sum(1 for flight in flights if isinstance(flight, dict))
    payload["flights"] = rows[:limit]
    # The airline logos for exactly these aircraft, as data: URIs (see the logo
    # section above for why the page never fetches them itself).
    payload["logos"] = airline_logos(payload["flights"], config)
    # The real airline/type pictures, same idea (see the image section above).
    payload["images"] = aircraft_images(payload["flights"], config)
    if total > limit:
        payload["warning"] = f"Showing {limit} of {total} aircraft overhead."

    # View range: the display's own setting, else fit the sensor's box and the
    # furthest aircraft (so nothing is ever drawn off-screen).
    try:
        configured = float(str(config.get("rangeKm", "0")))
    except (TypeError, ValueError):
        configured = 0.0
    furthest = max([r["distanceKm"] for r in rows] + [1.0])
    automatic = max(box_km or 0.0, furthest) * 1.12 + 0.4
    view = configured if configured > 0 else max(automatic, 1.0)
    view = max(view, furthest * 1.05, 0.5)
    payload["rangeKm"] = round(configured, 2) if configured > 0 else 0
    payload["viewKm"] = round(view, 2)
    payload["rings"] = ring_set(view)
    return payload


def ring_set(view_km: float):
    """Two or three 'nice' range rings that fit inside the view."""
    nice = (0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500)
    target = view_km / 3.0
    step = nice[0]
    for candidate in nice:
        if candidate <= target:
            step = candidate
    inside = [round(step * multiplier, 2) for multiplier in (1, 2, 3)
              if step * multiplier <= view_km * 1.02]
    return inside or [round(view_km, 2)]


# --------------------------------------------------------------------------- #
# Display store
# --------------------------------------------------------------------------- #
def load_displays() -> list:
    try:
        value = json.loads(DATA_FILE.read_text())
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_displays(displays: list) -> None:
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(displays, indent=2) + "\n")
    temporary.replace(DATA_FILE)


def clean_display(payload: dict, existing: dict = None) -> dict:
    """Validate and normalise one display, so the store can never hold a config
    the renderer cannot handle. Every field falls back to DEFAULTS, so adding a
    setting later needs no migration."""
    existing = existing or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("A display name is required.")
    raw_id = str(existing["id"] if existing else (payload.get("id") or name)).lower()
    identifier = re.sub(r"[^a-z0-9-]+", "-", raw_id).strip("-")[:48]
    if not identifier:
        raise ValueError("That display name does not produce a usable ID.")
    values = {"id": identifier, "name": name[:80]}

    combined = {key: payload.get(key, existing.get(key, default)) for key, default in DEFAULTS.items()}
    combined["entityId"] = as_text(combined.get("entityId") or default_entity(), 120)
    if combined["centreMode"] not in CENTRE_KEYS:
        combined["centreMode"] = "home"
    if combined["sortBy"] not in SORT_KEYS:
        combined["sortBy"] = "nearest"
    if combined["units"] not in UNIT_KEYS:
        combined["units"] = "metric"
    if combined["airlineLogo"] not in LOGO_KEYS:
        combined["airlineLogo"] = DEFAULTS["airlineLogo"]
    if combined["typeGraphic"] not in TYPE_GRAPHIC_KEYS:
        combined["typeGraphic"] = DEFAULTS["typeGraphic"]
    if combined["typeImage"] not in TYPE_IMAGE_KEYS:
        combined["typeImage"] = DEFAULTS["typeImage"]

    latitude = as_float(combined.get("latitude"))
    longitude = as_float(combined.get("longitude"))
    if combined["centreMode"] == "custom":
        if latitude is None or longitude is None or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            raise ValueError("Custom centre needs a latitude (-90 to 90) and longitude (-180 to 180).")
    combined["latitude"] = "" if latitude is None else f"{latitude:.6f}"
    combined["longitude"] = "" if longitude is None else f"{longitude:.6f}"

    for key, low, high in (("rangeKm", 0, 500), ("maxFlights", 1, 20), ("refreshInterval", 5, 3600)):
        try:
            number = float(str(combined.get(key, DEFAULTS[key])))
        except (TypeError, ValueError):
            number = float(DEFAULTS[key])
        number = max(low, min(high, number))
        combined[key] = str(int(number)) if key != "rangeKm" else f"{number:g}"
    # Text sizes are percentages: 50% to 300% of the designed size.
    for key in SIZE_KEYS:
        try:
            number = float(str(combined.get(key, DEFAULTS[key])))
        except (TypeError, ValueError):
            number = float(DEFAULTS[key])
        combined[key] = str(int(max(50, min(300, number))))

    accent = as_text(combined.get("accent"), 16)
    combined["accent"] = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent or "") else DEFAULTS["accent"]
    combined["title"] = as_text(combined.get("title"), 60)
    for key in ("hideOnGround", "showRoute", "showType", "showSpeed", "showDistance",
                "showTrails", "showRings", "showSweep", "showPulse") + DETAIL_FLAGS:
        combined[key] = "true" if truthy(combined.get(key)) else "false"
    values.update(combined)
    return values


def default_entity() -> str:
    return as_text(option("flight_entity") or OPTION_DEFAULTS["flight_entity"], 120)


def build_config(display: dict) -> dict:
    """The settings the display page receives: defaults + stored display + the
    add-on's shared access token (so the page's own fetches stay authorised)."""
    config: dict[str, object] = dict(DEFAULTS)
    config.update({key: value for key, value in display.items() if key in DEFAULTS or key == "id"})
    if not config.get("entityId"):
        config["entityId"] = default_entity()
    config["name"] = display.get("name", "")
    config["port"] = PORT
    config["authToken"] = access_token()
    config["homeLatitude"] = option("home_latitude")
    config["homeLongitude"] = option("home_longitude")
    return config


_ICONS: dict | None = None


def aircraft_icons_json() -> str:
    """The vendored aircraft silhouettes, as JSON for the display page to inline.

    Embedded rather than fetched on purpose: the page must work with no network
    at all, and the admin's unsaved-settings preview renders it from `srcdoc`,
    where there is no base URL to fetch anything from. ~64 KB of paths, read once
    (the file ships inside the app and cannot change while it runs).

    Only `</` is escaped — a JSON string inside <script> ends the block if it
    carries one, and the SVG markup has plenty of `<` that need not be escaped.
    """
    global _ICONS
    if _ICONS is None:
        try:
            _ICONS = json.loads((DISPLAY_FILE.parent / "aircraft_icons.json").read_text())
        except (OSError, json.JSONDecodeError):
            _ICONS = {}
    return json.dumps(_ICONS, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")


def render_display(display: dict, data: dict = None) -> str:
    config = build_config(display)
    injected = json.dumps(config).replace("<", "\\u003c")
    flags = f"<script>window.KIOSK_FLIGHT_CONFIG={injected};</script>"
    flags += f"<script>window.KIOSK_FLIGHT_ICONS={aircraft_icons_json()};</script>"
    if data is not None:
        flags += ("<script>window.KIOSK_FLIGHT_DATA="
                  + json.dumps(data).replace("<", "\\u003c") + ";</script>")
    return DISPLAY_FILE.read_text().replace("</head>", flags + "</head>", 1)


# --------------------------------------------------------------------------- #
# The logo folder, seen from the admin page
#
# List it, save an upload into it, delete a file from it — plus, for every
# airline the sensor is reporting right now, where its logo would come from.
# Uploads arrive as base64 inside JSON rather than as multipart: the admin page
# posts JSON everywhere else, and a filename that came from a browser is the one
# thing this app must never take on trust.
# --------------------------------------------------------------------------- #
def logo_thumb(path, size: int) -> str:
    """A data URI for the panel's preview — only for artwork small enough that
    sending it to the admin page costs nothing (the ones that are 300 KB are
    the ones the display itself would downsample anyway)."""
    if size > 60_000:
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    mime = guess_mime(data)
    if not mime:
        return ""
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


def logo_files() -> list:
    """Every file in the logo folder, in sort order.

    Deliberately not logo_library(): that is keyed BY KEY, so two files whose
    names reduce to the same slug ("Aer Lingus.png" and "aerlingus.png") collapse
    into one entry and the loser would be invisible — impossible to delete from
    the panel while sitting in the folder.
    """
    try:
        entries = sorted(logo_directory().iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return []
    return [path for path in entries
            if path.is_file() and path.suffix.lower() in LOGO_EXTENSIONS]


def logo_report() -> dict:
    """{directory, files, airlines, error} for the admin page's logo panel.

    The airline list is the app's DEFAULT display config — the option's sensor and
    the default number of aircraft — not any one display's, so a logo can be added
    for an airline the panel is showing even when no display happens to show it.
    Asking here also warms the fetch cache for those airlines.
    """
    payload = flight_rows(build_config({}))
    sources, order = {}, []
    for row in payload.get("flights") or []:
        name = as_text(row.get("airline"), 40) or as_text(row.get("airlineCode"), 8)
        if not name or name in sources:
            continue
        path = logo_file(row)
        if path:
            source, detail = "yours", path.name
        elif logo_bytes(row):
            source, detail = "fetched", ""
        else:
            source, detail = "code", ""
        sources[name] = source
        order.append({"name": name, "code": as_text(row.get("airlineCode"), 8),
                      "source": source, "file": detail})
    used = {}
    for entry in order:
        if entry["file"]:
            used.setdefault(entry["file"], []).append(entry["name"])
    library = logo_library()
    files = []
    for path in logo_files():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        key = logo_slug(path.stem)
        files.append({"file": path.name, "key": key, "bytes": size,
                      "active": library.get(key) == path,
                      "airlines": sorted(used.get(path.name, [])),
                      "thumb": logo_thumb(path, size)})
    return {"directory": str(logo_directory()), "files": files, "airlines": order,
            "fetched": sum(1 for entry in order if entry["source"] == "fetched"),
            "sensor": as_text(payload.get("entityId"), 120), "error": payload.get("error", "")}


def logo_upload(form: dict) -> dict:
    """Write one uploaded logo into the folder. Raises ValueError with a reason."""
    name = as_text(form.get("airline"), 60)
    code = re.sub(r"[^A-Za-z0-9]", "", as_text(form.get("code"), 8)).upper()
    if not name and not code:
        raise ValueError("Give the airline's name, or its code.")
    encoded = as_text(form.get("data"), 4 * LOGO_UPLOAD_MAX)
    if not encoded:
        raise ValueError("Choose an image file first.")
    body = encoded.split(",", 1)[1] if encoded.startswith("data:") else encoded
    try:
        data = base64.b64decode(body, validate=True)
    except ValueError:
        raise ValueError("That image could not be read.")
    mime = guess_mime(data)
    if not mime or not data:
        raise ValueError("That file is not a PNG, JPEG, WebP or SVG image.")
    if len(data) > LOGO_UPLOAD_MAX:
        raise ValueError(f"That image is {len(data) // 1024} KB; the limit is "
                         f"{LOGO_UPLOAD_MAX // 1024} KB.")
    stem = code if code else logo_slug(name)
    if not re.fullmatch(r"[A-Za-z0-9]{2,24}", stem):
        raise ValueError("That name has no letters or digits to file it under.")
    target = logo_directory() / (stem + EXT_BY_MIME.get(mime, ".png"))
    try:
        logo_directory().mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
    except OSError as error:
        raise ValueError(f"Could not write to {logo_directory()}: {error}")
    _logo_index["at"] = 0.0          # the next lookup must see the new file
    return {"file": target.name, "key": logo_slug(target.stem), "bytes": len(data),
            "airline": name or code}


def logo_delete(name: str) -> dict:
    """Remove one file from the folder. Raises ValueError with a reason."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()&'-]{0,79}\.(png|jpg|jpeg|webp|svg)",
                        name, re.IGNORECASE):
        raise ValueError("That is not a logo filename.")
    path = logo_directory() / name
    if not path.is_file():
        raise ValueError(f"{name} is not in the logo folder.")
    try:
        path.unlink()
    except OSError as error:
        raise ValueError(f"Could not delete {name}: {error}")
    _logo_index["at"] = 0.0
    return {"deleted": name}


# --------------------------------------------------------------------------- #
# Admin page
# --------------------------------------------------------------------------- #
def admin_page() -> str:
    return r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kiosk Flight Displays</title><style>
body{max-width:940px;margin:0 auto;padding:28px;font:16px system-ui,sans-serif;background:#0f172a;color:#f8fafc}
h1{margin-bottom:4px}p{color:#cbd5e1}section{margin:24px 0;padding:22px;border:1px solid #334155;border-radius:12px;background:#1e293b}
form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
label{display:grid;gap:5px;color:#cbd5e1;font-size:.9rem}
input,select,button{padding:10px;border-radius:7px;font:inherit}
input,select{border:1px solid #64748b;background:#0f172a;color:white}
button{border:0;background:#38bdf8;color:#082f49;font-weight:700;cursor:pointer}
.wide{grid-column:1/-1}h2{font-size:1.1rem}h3{grid-column:1/-1;margin:8px 0 -4px;font-size:.95rem;color:#7dd3fc}
.row{display:block;border-top:1px solid #334155;padding:15px 0}.row:first-child{border:0}
.dash-top{display:flex;align-items:baseline;gap:12px;margin-bottom:8px}.dash-top small{color:#94a3b8}
.link-line{display:flex;align-items:center;gap:10px;margin:6px 0}
.link-label{min-width:88px;color:#94a3b8;font-size:.84rem;flex-shrink:0}
.link-url{flex:1;color:#7dd3fc;font-size:.86rem;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.link-url:hover{text-decoration:underline}
.copy{flex-shrink:0;background:#334155;color:#e2e8f0;padding:6px 12px;font-size:.9rem;cursor:pointer;border-radius:6px;border:0}
.copy:hover{background:#475569}.dash-actions{display:flex;gap:10px;margin-top:10px}.dash-actions button{padding:8px 18px}
.secondary{background:#334155;color:#fff}.danger{background:#b91c1c;color:#fff}
.check{display:flex;align-items:center;gap:9px;color:#e2e8f0}.check input{width:auto;margin:0;accent-color:#38bdf8}
.hint{grid-column:1/-1;color:#94a3b8;font-size:.85rem;margin:-6px 0 0}
.sensor-status{grid-column:1/-1;display:flex;align-items:center;gap:10px;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:9px 13px;font-size:.88rem;color:#cbd5e1}
.sensor-status b{color:#f8fafc}.sensor-status.error{border-color:#b91c1c;color:#fca5a5}
.modal-overlay{position:fixed;inset:0;z-index:100;display:grid;justify-items:center;align-items:start;padding:20px;background:rgba(3,7,18,.85);overflow-y:auto}
.modal-overlay[hidden]{display:none}
.modal-content{width:min(100%,690px);max-height:calc(100vh - 40px);overflow:auto;padding:24px;background:#1e293b;border:1px solid #334155;border-radius:14px}
.modal-buttons{display:flex;gap:10px;grid-column:1/-1}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1e293b;border:1px solid #38bdf8;color:#f8fafc;padding:12px 20px;border-radius:9px;z-index:300;display:flex;gap:9px}
.checkmark{color:#4ade80}
.preview-overlay{position:fixed;inset:0;z-index:200;background:rgba(3,7,18,.9);display:grid;place-items:center;padding:20px}
.preview-overlay[hidden]{display:none}
.preview-frame-wrap{width:min(96vw,1400px);height:min(92vh,1000px);display:flex;flex-direction:column;background:#0b1220;border:1px solid #334155;border-radius:12px;overflow:hidden}
.preview-frame-bar{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:#1e293b;border-bottom:1px solid #334155}
.preview-close{background:#334155;color:#e2e8f0}.preview-frame{flex:1;border:0;width:100%;background:#050912}
.preview-hint{grid-column:1/-1;color:#fca5a5;font-size:.88rem}
.slider-field{display:grid;gap:6px}.slider-row{display:flex;align-items:center;gap:12px}
.slider-row input[type=range]{flex:1;padding:0;border:0;background:transparent;height:24px}
.slider-row output{min-width:30px;text-align:center;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:4px 6px;font-size:.85rem}
.sensor-summary{margin:0 0 12px;color:#94a3b8;font-size:.88rem}
.sensor-summary b{color:#f8fafc}
.sensor-summary.error{color:#fca5a5}
@media(max-width:620px){form{grid-template-columns:1fr}}
.logo-row{display:flex;align-items:center;gap:12px}
.logo-thumb{max-height:34px;max-width:150px;background:rgba(238,245,255,.94);border-radius:5px;padding:3px 5px}
.logo-row .logo-grow{flex:1;min-width:0}
</style></head><body>
<h1>Kiosk Flight Displays</h1>
<p>Full-screen displays of the aircraft flying over your house. Data comes from the
<strong>Flightradar24</strong> integration you have running in Home Assistant (Settings → Devices &amp; services)
and is read server-side, so a kiosk on a network with no internet needs nothing else.</p>
<p class="hint">Aircraft type icons by <a href="https://adsb-radar.com" rel="noopener">ADS-B Radar for macOS</a>
(used with a backlink, as its licence asks).</p>
<section><h2>New display</h2><button id="new-display">＋ Add a full-screen flight display</button></section>
<section><h2>Your displays</h2><p class="sensor-summary" id="sensor-summary">Checking the sensor…</p>
<div id="list">Loading…</div></section>
<section><h2>Airline logos</h2>
<p>Each airline's own mark is fetched once by the app and kept, but a file in the folder below always
wins — which is how an airline with no published logo, or one whose mark you would rather not show,
gets its own artwork. Name the file after the airline (<code>Aer Lingus.png</code>) or its code
(<code>EIN.png</code>): case, spaces and the extension are ignored. A logo added here is stored in
that folder, so it survives updates and travels with a backup.</p>
<div class="link-line"><span class="link-label">Folder</span>
  <a class="link-url" id="logo-dir" href="#"></a>
  <button class="copy" id="logo-dir-copy" title="Copy the path">⧉</button></div>
<div class="row">
  <label>Airline or code<input id="logo-name" placeholder="Aer Lingus — or EIN"></label>
  <label>Image file<input type="file" id="logo-file"
    accept="image/png,image/jpeg,image/webp,image/svg+xml"></label>
  <div class="dash-actions"><button id="logo-save">Save logo</button></div>
  <p class="preview-hint" id="logo-hint"></p>
  <p class="hint">Two to four letters are taken as the airline's code; anything longer as its name.
  PNG, JPEG, WebP or SVG, up to 400 KB. The file is named after whichever you gave, so a later
  upload for the same airline replaces it.</p>
</div>
<h3>Flying over now</h3>
<p class="sensor-summary" id="logo-status">Checking…</p>
<div id="logo-airlines"></div>
<h3>In the folder</h3>
<div id="logo-list">Loading…</div>
</section>
<div id="editor-modal" class="modal-overlay" hidden><div class="modal-content">
<h2 id="modal-title">New display</h2>
<form id="editor">
<input id="edit-id" type="hidden">
<label>Name<input name="name" required placeholder="Overhead"></label>
<label>Title shown on screen<input name="title" placeholder="AIRCRAFT OVERHEAD"></label>
<div class="sensor-status" id="sensor-status">Checking the sensor…</div>
<label class="wide">Sensor<select name="entityId" id="entity-select"></select></label>
<p class="hint wide"><strong>Which aircraft appear is decided by the Flightradar24 integration, not by this
app.</strong> A sensor only reports aircraft inside its own area and radius, and an aircraft it hides (below
its minimum altitude, say) never reaches this display. To change the area: Home Assistant → Settings →
Devices &amp; services → Flightradar24 → Configure. The radar range below is <em>zoom only</em>.</p>
<h3>What to show</h3>
<label class="slider-field">Aircraft shown<output id="max-out">6</output>
  <div class="slider-row"><input type="range" name="maxFlights" min="1" max="20" step="1" value="6"
    data-out="max-out" oninput="syncOut(this)"></div></label>
<label>Sort by<select name="sortBy">
  <option value="nearest">Nearest first</option><option value="lowest">Lowest first</option>
  <option value="highest">Highest first</option><option value="fastest">Fastest first</option>
  <option value="callsign">Callsign (A–Z)</option></select></label>
<label>Units<select name="units">
  <option value="metric">Metric — km, m, km/h</option>
  <option value="aviation">Aviation — nautical miles, feet, knots</option>
  <option value="imperial">Imperial — miles, feet, mph</option></select></label>
<label>Radar range (km)<input name="rangeKm" type="number" min="0" max="500" step="0.5" placeholder="0">
  <span class="hint">Zoom only. 0 fits the view to the sensor's area and the furthest aircraft; a fixed
  value zooms out to that radius. It cannot bring in aircraft the sensor does not report.</span></label>
<label>Refresh every (seconds)<input name="refreshInterval" type="number" min="5" max="3600" step="1"></label>
<label class="check"><input type="checkbox" name="showRoute" data-flag> Origin → destination</label>
<label class="check"><input type="checkbox" name="showType" data-flag> Aircraft type</label>
<label class="check"><input type="checkbox" name="showSpeed" data-flag> Speed</label>
<label class="check"><input type="checkbox" name="showDistance" data-flag> Distance from the centre</label>
<label class="check"><input type="checkbox" name="hideOnGround" data-flag> Hide aircraft on the ground</label>
<label class="check"><input type="checkbox" name="showTrails" data-flag> Flight trails</label>
<label class="check"><input type="checkbox" name="showRings" data-flag> Range rings</label>
<label class="check"><input type="checkbox" name="showSweep" data-flag> Radar sweep</label>
<label class="check"><input type="checkbox" name="showPulse" data-flag> Home-marker ripple</label>
<h3>Single-aircraft dashboard</h3>
<p class="hint wide">A second, full-screen view for <strong>one</strong> aircraft: its flight number, altitude,
origin → destination, a top-down schematic of the type and the airline's code badge. It is the closest
aircraft, and it refreshes with the radar.</p>
<label class="check"><input type="checkbox" name="detailAlways" data-flag> This display IS the dashboard
  (standalone — no radar)</label>
<label class="check"><input type="checkbox" name="detailAuto" data-flag> Switch to it when only one aircraft is left</label>
<label class="check"><input type="checkbox" name="detailClick" data-flag> Open it when an aircraft is tapped
  (tap again to go back)</label>
<label>Aircraft image<select name="typeImage">
  <option value="silhouette">Drawn silhouette — nothing fetched</option>
  <option value="image">Real picture — the airline's livery on this type</option></select>
  <span class="hint">Looks for a picture of <em>this airline on this aircraft type</em>: first in the
  image library configured in the app options (<code>image_library</code>, default
  <code>/data/liveries</code>), then from a keyed API if <code>image_api_url</code> is set. Whatever it
  finds is kept under <code>/data</code> — never in the app's own files. When there is no picture, the
  silhouette above is shown instead, so the display never comes up empty.</span></label>
<label>Aircraft type graphic<select name="typeGraphic">
  <option value="top">Top-down — the plan view</option>
  <option value="side">Side view — a profile of the type</option></select>
  <span class="hint">Both sets are drawn locally and matched to the type the sensor reports
  (<code>aircraft_code</code>). The profile set distinguishes <em>class</em> — twin-jet, widebody,
  four-engine, regional, business jet, turboprop, light aircraft, helicopter, glider — so an A321
  and a 737 share a profile; anything with no profile (a drone, a balloon) keeps its plan view.</span></label>
<label>Airline logo<select name="airlineLogo">
  <option value="image">The airline's own logo</option>
  <option value="monogram">Code badge — its code, nothing fetched</option></select>
  <span class="hint">The logo is the one thing here that comes from the internet: the
  <em>app</em> fetches it once per airline and keeps it under <code>/data</code>, so it still shows on a
  kiosk with no internet. A file of your own in the logo folder (see
  <strong>Airline logos</strong> below) always wins over the fetched one. Until a logo has arrived —
  and on a Home Assistant with no internet at all — the badge shows the airline's code, on the same
  white plate the logo sits on.</span></label>
<p class="hint wide">Left unticked, the display is the radar exactly as before. The three are
independent: a standalone dashboard can also be one whose radar comes back when the sky fills up.</p>
<h3>Where it is centred</h3>
<label>Centre<select name="centreMode" id="centre-mode">
  <option value="home">Home — the zone.home location in Home Assistant</option>
  <option value="custom">Custom coordinates</option></select></label>
<label>Latitude<input name="latitude" placeholder="e.g. 40.7128"></label>
<label>Longitude<input name="longitude" placeholder="e.g. -74.0060"></label>
<label>Accent colour<input name="accent" type="color" value="#7dd3fc"></label>
<h3>Text sizes</h3>
<label class="slider-field">Overhead count<output id="size-count-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeCount" min="50" max="300" step="5" value="100"
    data-out="size-count-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Headline<output id="size-title-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeTitle" min="50" max="300" step="5" value="100"
    data-out="size-title-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Info lines (nearest, highest, rings)<output id="size-info-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeInfo" min="50" max="300" step="5" value="100"
    data-out="size-info-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Aircraft callsign<output id="size-callsign-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeCallsign" min="50" max="300" step="5" value="100"
    data-out="size-callsign-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Aircraft detail lines<output id="size-details-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeDetails" min="50" max="300" step="5" value="100"
    data-out="size-details-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Compass &amp; area labels<output id="size-grid-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeGrid" min="50" max="300" step="5" value="100"
    data-out="size-grid-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Single-aircraft dashboard<output id="size-detail-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeDetail" min="50" max="300" step="5" value="100"
    data-out="size-detail-out" oninput="syncOut(this)"></div></label>
<label class="slider-field">Credit line<output id="size-footer-out">100%</output>
  <div class="slider-row"><input type="range" name="sizeFooter" min="50" max="300" step="5" value="100"
    data-out="size-footer-out" oninput="syncOut(this)"></div></label>
<div class="preview-hint" id="preview-hint"></div>
<div class="modal-buttons">
  <button type="submit">Save display</button>
  <button type="button" class="secondary" id="preview-btn">Preview</button>
  <button type="button" class="secondary" id="cancel">Cancel</button>
</div>
</form></div></div>
<div id="preview-overlay" class="preview-overlay" hidden><div class="preview-frame-wrap">
  <div class="preview-frame-bar"><strong>Preview — unsaved settings</strong>
    <button class="preview-close" id="preview-close">Close</button></div>
  <iframe class="preview-frame" id="preview-frame" title="Display preview"></iframe></div></div>
<script>
var f=document.querySelector('#editor'),list=document.querySelector('#list'),modal=document.querySelector('#editor-modal'),
    modalTitle=document.querySelector('#modal-title'),cancel=document.querySelector('#cancel'),
    statusEl=document.querySelector('#sensor-status'),selectEl=document.querySelector('#entity-select'),
    centreEl=document.querySelector('#centre-mode'),hintEl=document.querySelector('#preview-hint'),
    summaryEl=document.querySelector('#sensor-summary');
var items=[];
// Display links must be RELATIVE (no origin): through HA ingress the path contains a
// per-session token, so an absolute URL copied from one session 401s for everyone else.
var base=location.pathname.replace(/\/$/,'');
var route=function(p){return base+'/'+p.replace(/^\//,'')};
// Direct (non-ingress) link for kiosks and devices without a Home Assistant session.
// Must point at the app's own port: building it from location.origin while viewing
// through ingress would wrap the ingress path again and 401.
var DIRECT_PORT='8096';
var ACCESS_TOKEN='__ACCESS_TOKEN__';
var directLink=function(p){return ACCESS_TOKEN
  ? location.protocol+'//'+location.hostname+':'+DIRECT_PORT+p+'?auth='+encodeURIComponent(ACCESS_TOKEN)
  : ''};
// Every admin fetch carries the token so the page also works on the direct URL.
function withAuth(p){return p+(p.indexOf('?')>=0?'&':'?')+'auth='+encodeURIComponent(ACCESS_TOKEN)}
async function request(path,options){
  options=options||{};options.headers=options.headers||{};
  if(ACCESS_TOKEN)options.headers['X-Access-Token']=ACCESS_TOKEN;
  var r=await fetch(route(path),options);var data=await r.json();
  if(!r.ok)throw Error(data.error||'Request failed');return data}
function field(name,value){var input=f.elements[name];if(input&&input.type!=='checkbox')input.value=value==null?'':value;
  else if(input)input.checked=String(value)!=='false'}
// Sliders carry their readout in data-out so one helper keeps every output in step.
function syncOut(input){var out=document.querySelector('#'+input.getAttribute('data-out'));if(!out)return;
  out.textContent=input.value+(input.name==='maxFlights'?'':'%')}
function syncOutputs(){var inputs=f.querySelectorAll('input[type=range]');
  for(var i=0;i<inputs.length;i++)syncOut(inputs[i])}
function resetForm(){
  f.reset();field('edit-id','');modalTitle.textContent='New display';hintEl.textContent='';
  for(var i=0;i<f.elements.length;i++){var el=f.elements[i];
    if(el.getAttribute&&el.getAttribute('data-flag')!==null&&el.type==='checkbox')el.checked=true}
  field('accent','#7dd3fc');field('maxFlights','6');field('rangeKm','0');field('refreshInterval','20');
  field('sortBy','nearest');field('units','metric');field('centreMode','home');field('showType','false');
  field('detailAlways','false');field('detailAuto','false');field('detailClick','false');
  field('airlineLogo','image');field('typeGraphic','top');field('typeImage','silhouette');
  syncOutputs();
}
function syncFlags(){for(var i=0;i<f.elements.length;i++){var el=f.elements[i];
  if(el.getAttribute&&el.getAttribute('data-flag')!==null&&el.type==='checkbox')el.value=el.checked?'true':'false'}}
function openModal(display){
  resetForm();
  if(display){
    for(var key in display){if(key==='id')continue;
      var el=f.elements[key];
      if(el&&el.type==='checkbox'){el.checked=String(display[key])!=='false';el.value=el.checked?'true':'false'}
      else field(key,display[key])}
    field('edit-id',display.id);modalTitle.textContent='Edit: '+display.name}
  syncOutputs();
  modal.hidden=false;
}
function closeModal(){modal.hidden=true;resetForm()}
function showToast(message){var toast=document.createElement('div');toast.className='toast';
  toast.innerHTML='<span class="checkmark">✓</span>'+message;document.body.append(toast);
  setTimeout(function(){toast.remove()},1600)}
async function loadSensors(){
  statusEl.className='sensor-status';statusEl.textContent='Checking the sensor…';
  summaryEl.className='sensor-summary';summaryEl.textContent='Checking the sensor…';
  var data;
  try{data=await request('/api/sensors')}
  catch(e){statusEl.className='sensor-status error';statusEl.textContent=e.message;
    summaryEl.className='sensor-summary error';summaryEl.textContent=e.message;return}
  selectEl.innerHTML='';
  if(!data.sensors.length){
    var missing='No sensor with a "flights" list found in Home Assistant. Install the Flightradar24 integration first.';
    statusEl.className='sensor-status error';statusEl.textContent=missing;
    summaryEl.className='sensor-summary error';summaryEl.textContent=missing;
    return}
  var parts=[];
  for(var i=0;i<data.sensors.length;i++){
    var s=data.sensors[i];var option=document.createElement('option');
    option.value=s.entity_id;
    option.textContent=s.name+' — '+s.airborne+' airborne of '+s.count
      +(s.area?', '+s.area+' area':'')+' ('+s.entity_id+')';
    selectEl.append(option);
    if(i<3)parts.push('<b>'+s.airborne+'</b> airborne of '+s.count+' in '
      +s.entity_id.replace('sensor.flightradar24_','')+(s.area?' ('+s.area+')':''))}
  // Whether anything is actually overhead right now, right on the list page: an
  // empty display is normal, and this is how you tell "quiet sky" from "broken".
  summaryEl.innerHTML='Sensor reports right now: '+parts.join(' · ')
    +'. Each sensor only looks at its own area, so <b>0 is normal</b> — widen the area in the '
    +'Flightradar24 integration if you want more traffic.';
  var want=f.elements['edit-id'].value?f.elements['entityId'].value:'';
  if(!want)want=data.default_entity;
  for(var j=0;j<selectEl.options.length;j++)if(selectEl.options[j].value===want)selectEl.value=want;
  var chosen=null;
  for(var k=0;k<data.sensors.length;k++)if(data.sensors[k].entity_id===selectEl.value)chosen=data.sensors[k];
  statusEl.innerHTML=chosen
    ? 'Sensor live: <b>'+chosen.airborne+'</b> airborne of <b>'+chosen.count+'</b> in the area'
      +(chosen.area?' ('+chosen.area+')':'')+' · state '+chosen.state
    : 'Pick a sensor below.';
}
function render(){
  list.innerHTML=items.length?'':'<p>No displays yet.</p>';
  for(var i=0;i<items.length;i++){
    var d=items[i];var row=document.createElement('div');row.className='row';
    var ingress=route('/display/'+d.id),direct=directLink('/display/'+d.id);
    row.innerHTML='<div class="dash-top"><strong>'+d.name+'</strong><small>'+d.entityId+'</small></div>'
      +'<div class="link-line"><span class="link-label">Ingress</span><a class="link-url" href="'+ingress
      +'" target="_blank" rel="noopener">'+ingress+'</a><button class="copy" type="button" data-copy="'
      +ingress+'" title="Copy link">⧉</button></div>'
      +(direct?'<div class="link-line"><span class="link-label">Direct</span><a class="link-url" href="'+direct
      +'" target="_blank" rel="noopener">'+direct+'</a><button class="copy" type="button" data-copy="'+direct
      +'" title="Copy direct link (token-authenticated, for kiosks)">⧉</button></div>':'')
      +'<div class="dash-actions"><button class="secondary">Edit</button><button class="danger">Delete</button></div>';
    row.querySelector('.secondary').onclick=function(d){return function(){openModal(d);loadSensorsFor(d)}}(d);
    var delBtn=row.querySelector('.danger');
    delBtn.onclick=function(d,btn){return async function(){
      // Native confirm() is silently blocked inside HA's sandboxed iframe — two taps instead.
      if(btn.dataset.armed){
        btn.disabled=true;
        try{await request('/api/displays/'+d.id,{method:'DELETE'});showToast('Deleted');load()}
        catch(e){showToast(e.message);btn.disabled=false;btn.dataset.armed='';btn.textContent='Delete'}
      }else{btn.dataset.armed='1';btn.textContent='Really delete?';
        setTimeout(function(){if(btn.isConnected&&btn.dataset.armed){btn.dataset.armed='';btn.textContent='Delete'}},3000)}
    }}(d,delBtn);
    var copies=row.querySelectorAll('.copy');
    for(var c=0;c<copies.length;c++)copies[c].onclick=function(btn){return async function(){
      var url=btn.dataset.copy;
      try{await navigator.clipboard.writeText(url)}
      catch(e){var ta=document.createElement('textarea');ta.value=url;document.body.append(ta);ta.select();
        document.execCommand('copy');ta.remove()}
      btn.textContent='✓';setTimeout(function(){btn.textContent='⧉'},1200)
    }}(copies[c]);
    list.append(row);
  }
}
async function loadSensorsFor(display){
  await loadSensors();
  if(display&&display.entityId){selectEl.value=display.entityId}
}
async function load(){items=await request('/api/displays');render()}
f.onsubmit=async function(e){
  e.preventDefault();syncFlags();
  var id=f.elements['edit-id'].value;
  var data=Object.fromEntries(new FormData(f));
  try{
    await request(id?'/api/displays/'+id:'/api/displays',
      {method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    showToast(id?'Saved':'Created');closeModal();load()
  }catch(error){hintEl.textContent=error.message}
};
document.querySelector('#preview-btn').onclick=async function(){
  syncFlags();hintEl.textContent='Rendering…';
  try{
    var data=Object.fromEntries(new FormData(f));
    var r=await fetch(route('/api/preview'),{method:'POST',
      headers:{'Content-Type':'application/json','X-Access-Token':ACCESS_TOKEN},body:JSON.stringify(data)});
    var payload=await r.json();
    if(!r.ok){hintEl.textContent=payload.error||'Preview failed';return}
    hintEl.textContent=payload.warning||'';
    document.querySelector('#preview-frame').srcdoc=payload.html;
    document.querySelector('#preview-overlay').hidden=false;
  }catch(error){hintEl.textContent='Preview failed: '+error.message}
};
document.querySelector('#preview-close').onclick=function(){
  document.querySelector('#preview-overlay').hidden=true;
  document.querySelector('#preview-frame').srcdoc=''};
document.querySelector('#editor-modal').addEventListener('click',function(e){
  if(e.target===document.querySelector('#editor-modal'))closeModal()});
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'){document.querySelector('#preview-overlay').hidden=true;closeModal()}});
cancel.onclick=closeModal;
document.querySelector('#new-display').onclick=async function(){openModal(null);await loadSensors()};
centreEl.addEventListener('change',function(){
  var custom=centreEl.value==='custom';
  f.elements['latitude'].disabled=!custom;f.elements['longitude'].disabled=!custom});
// --- airline logos -------------------------------------------------------- //
var logoNameEl=document.querySelector('#logo-name'),logoFileEl=document.querySelector('#logo-file'),
    logoHintEl=document.querySelector('#logo-hint'),logoListEl=document.querySelector('#logo-list'),
    logoAirEl=document.querySelector('#logo-airlines'),logoStatusEl=document.querySelector('#logo-status'),
    logoDirEl=document.querySelector('#logo-dir');
function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function kbytes(n){return n>=1024?Math.round(n/1024)+' KB':n+' B'}
function logoSource(a){
  if(a.source==='yours')return '<b>your logo</b> — '+esc(a.file);
  if(a.source==='fetched')return 'fetched from Flightradar24';
  return 'nothing yet — the code badge is shown'}
function logoNote(f){
  if(!f.active)return 'not used: another file answers for <code>'+esc(f.key)+'</code>';
  if(f.airlines.length)return 'answers for '+esc(f.airlines.join(', '));
  return 'not used by an aircraft overhead right now — key '+esc(f.key)}
async function loadLogos(){
  var data;
  try{data=await request('/api/logos')}
  catch(e){logoStatusEl.className='sensor-summary error';
    logoStatusEl.textContent='Could not read the logo folder: '+e.message;return}
  logoDirEl.textContent=data.directory;
  document.querySelector('#logo-dir-copy').dataset.copy=data.directory;
  if(data.error){logoStatusEl.className='sensor-summary error';logoStatusEl.textContent=data.error}
  else{logoStatusEl.className='sensor-summary';
    logoStatusEl.textContent=(data.airlines.length?data.airlines.length+' airline'
      +(data.airlines.length===1?'':'s')+' in '+data.sensor:'no aircraft reported by '+(data.sensor||'the sensor'))
      +(data.fetched?', '+data.fetched+' fetched':'');}
  logoAirEl.innerHTML='';
  for(var i=0;i<data.airlines.length;i++){
    var a=data.airlines[i],row=document.createElement('div');row.className='row';
    row.innerHTML='<div class="dash-top"><strong>'+esc(a.name)+'</strong>'
      +'<small>'+esc(a.code||'')+'</small></div><p class="hint">'+logoSource(a)+'</p>';
    var button=document.createElement('button');button.className='secondary';
    button.textContent=a.source==='yours'?'Replace this logo':'Give this airline a logo';
    (function(name,code){button.onclick=function(){
      logoNameEl.value=name||code;logoNameEl.focus();logoHintEl.textContent='';
      if(code&&!name)logoNameEl.value=code}}(a.name,a.code));
    row.append(button);logoAirEl.append(row);
  }
  logoListEl.innerHTML='';
  if(!data.files.length){
    logoListEl.innerHTML='<p class="hint">Nothing here yet: every logo on the displays comes from '
      +'Flightradar24. Drop a file in, or add one above.</p>';return}
  for(var j=0;j<data.files.length;j++){
    var file=data.files[j],line=document.createElement('div');line.className='row logo-row';
    line.innerHTML=(file.thumb?'<img class="logo-thumb" alt="" src="'+file.thumb+'">':'')
      +'<div class="logo-grow"><div class="dash-top"><strong>'+esc(file.file)+'</strong>'
      +'<small>'+kbytes(file.bytes)+'</small></div><p class="hint">'+logoNote(file)+'</p></div>';
    var del=document.createElement('button');del.className='danger';del.textContent='Delete';
    (function(btn,name){btn.onclick=function(){
      if(!btn.dataset.armed){btn.dataset.armed='1';btn.textContent='Really delete?';
        setTimeout(function(){if(btn.isConnected&&btn.dataset.armed){btn.dataset.armed='';btn.textContent='Delete'}},3000);
        return}
      request('/api/logos/'+encodeURIComponent(name),{method:'DELETE'}).then(function(){
        showToast('Deleted '+name);loadLogos()}).catch(function(e){logoHintEl.textContent=e.message})
    }}(del,file.file));
    line.append(del);logoListEl.append(line);
  }
}
document.querySelector('#logo-save').onclick=async function(){
  var who=logoNameEl.value.trim(),file=logoFileEl.files[0];
  logoHintEl.textContent='';
  if(!who){logoHintEl.textContent='Give the airline name or its code.';return}
  if(!file){logoHintEl.textContent='Choose an image file.';return}
  var body={data:''};
  if(/^[A-Za-z0-9]{2,4}$/.test(who))body.code=who;else body.airline=who;
  try{
    body.data=await new Promise(function(resolve,reject){
      var reader=new FileReader();reader.onload=function(){resolve(reader.result)};
      reader.onerror=function(){reject(Error('Could not read that file.'))};
      reader.readAsDataURL(file)});
    var saved=await request('/api/logos',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    showToast('Saved '+saved.file);logoFileEl.value='';logoHintEl.textContent='';
    loadLogos();
  }catch(e){logoHintEl.textContent=e.message}
};
document.querySelector('#logo-dir-copy').onclick=async function(){
  var btn=this,text=btn.dataset.copy||'';
  try{await navigator.clipboard.writeText(text)}
  catch(e){var ta=document.createElement('textarea');ta.value=text;document.body.append(ta);
    ta.select();document.execCommand('copy');ta.remove()}
  btn.textContent='✓';setTimeout(function(){btn.textContent='⧉'},1200)};
loadSensors();load();loadLogos();
</script></body></html>"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "KioskFlight/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        print(format % args, flush=True)

    # -- plumbing ---------------------------------------------------------- #
    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, value, status=200):
        data = value.encode()
        # no-store is deliberate: a cached display page is exactly what keeps a
        # kiosk stuck on old, broken client code after an update.
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode() or "{}")
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    @property
    def path_only(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def is_ingress(self) -> bool:
        return bool(self.headers.get("X-Hassio-Ingress")
                    or self.headers.get("X-Forwarded-For")
                    or self.headers.get("X-Forwarded-Host"))

    def authorized(self) -> bool:
        if self.is_ingress():
            return True
        expected = access_token()
        if not expected:
            return True        # no token file (dev box): fail OPEN, don't lock yourself out
        supplied = (parse_qs(urlparse(self.path).query).get("auth", [""])[0]
                    or self.headers.get("X-Access-Token", ""))
        return secrets.compare_digest(supplied, expected)

    def deny(self):
        self.send_json({"error": "Access token required (?auth=… or X-Access-Token)."}, 401)

    def find_display(self, identifier: str):
        for display in load_displays():
            if display.get("id") == identifier:
                return display
        return None

    # -- GET --------------------------------------------------------------- #
    def do_GET(self):
        path = self.path_only
        if path in ("/health", "/api/health"):
            # Unauthenticated readiness probe: HA and uptime checks must not need a token.
            return self.send_json({"ok": True, "supervisor": bool(SUPERVISOR_TOKEN)})
        if not self.authorized():
            return self.deny()
        if path == "/":
            return self.send_html(admin_page().replace("__ACCESS_TOKEN__", access_token()))
        if path == "/api/displays":
            return self.send_json(load_displays())
        if path == "/api/sensors":
            return self.sensors()
        if path == "/api/options":
            return self.send_json({"options": options(), "defaults": DEFAULTS,
                                   "default_entity": default_entity(),
                                   "supervisor_token": bool(SUPERVISOR_TOKEN)})
        if path == "/api/logos":
            return self.send_json(logo_report())
        match = re.fullmatch(r"/display/([A-Za-z0-9-]+)", path)
        if match:
            display = self.find_display(match.group(1))
            if not display:
                return self.send_html("<h1>Display not found</h1>", 404)
            return self.send_html(render_display(display))
        match = re.fullmatch(r"/api/flights/([A-Za-z0-9-]+)", path)
        if match:
            display = self.find_display(match.group(1))
            if not display:
                return self.send_json({"error": "Display not found."}, 404)
            return self.send_json(flight_rows(build_config(display)))
        # The admin page requests previews with a plain GET, so this route must live
        # in do_GET too — a POST-only variant 404s inside the preview iframe.
        match = re.fullmatch(r"/api/preview/([A-Za-z0-9-]+)", path)
        if match:
            display = self.find_display(match.group(1))
            if not display:
                return self.send_json({"error": "Display not found."}, 404)
            return self.preview(display)
        return self.send_json({"error": "Not found"}, 404)

    # -- writes ------------------------------------------------------------ #
    def do_POST(self):
        if not self.authorized():
            return self.deny()
        path = self.path_only
        if path == "/api/displays":
            payload = self.read_body()
            try:
                display = clean_display(payload)
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            displays = load_displays()
            if any(item.get("id") == display["id"] for item in displays):
                return self.send_json({"error": "A display with that name already exists."}, 409)
            displays.append(display)
            save_displays(displays)
            return self.send_json(display, 201)
        if path == "/api/preview":
            try:
                display = clean_display(self.read_body())
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            return self.preview(display)
        if path == "/api/logos":
            try:
                saved = logo_upload(self.read_body())
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            return self.send_json(saved, 201)
        return self.send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        if not self.authorized():
            return self.deny()
        match = re.fullmatch(r"/api/displays/([A-Za-z0-9-]+)", self.path_only)
        if not match:
            return self.send_json({"error": "Not found"}, 404)
        existing = self.find_display(match.group(1))
        if not existing:
            return self.send_json({"error": "Display not found."}, 404)
        try:
            updated = clean_display(self.read_body(), existing)
        except ValueError as error:
            return self.send_json({"error": str(error)}, 400)
        displays = [updated if item.get("id") == existing["id"] else item for item in load_displays()]
        save_displays(displays)
        return self.send_json(updated)

    def do_DELETE(self):
        if not self.authorized():
            return self.deny()
        match = re.fullmatch(r"/api/displays/([A-Za-z0-9-]+)", self.path_only)
        if not match:
            match = re.fullmatch(r"/api/logos/([^/]+)", self.path_only)
            if match:
                try:
                    removed = logo_delete(unquote(match.group(1)))
                except ValueError as error:
                    return self.send_json({"error": str(error)}, 400)
                return self.send_json(removed)
        if not match:
            return self.send_json({"error": "Not found"}, 404)
        remaining = [item for item in load_displays() if item.get("id") != match.group(1)]
        save_displays(remaining)
        return self.send_json({"deleted": match.group(1)})

    # -- shared handlers --------------------------------------------------- #
    def preview(self, display: dict):
        """Render with the data embedded: an iframe loaded from srcdoc has no base
        URL, so the page must not have to fetch anything (or carry the token)."""
        data = flight_rows(build_config(display))
        return self.send_json({"html": render_display(display, data),
                               "warning": data.get("error") or data.get("warning") or "",
                               "total": data.get("total", 0)})

    def sensors(self):
        """Every Home Assistant entity carrying positioned aircraft in a `flights` list.

        Airport arrival/departure sensors publish schedule rows with no latitude, and
        the most-tracked switch publishes an empty list — neither can be drawn, so they
        are left out of the picker rather than offered as a dead end.
        """
        found = []
        try:
            states = supervisor_states()
        except ValueError as error:
            return self.send_json({"error": str(error), "sensors": [],
                                   "default_entity": default_entity()})
        for entity_id, state in sorted(states.items()):
            if not entity_id.startswith("sensor."):
                continue
            attributes = state.get("attributes") or {}
            flights = attributes.get("flights")
            if not isinstance(flights, list):
                continue
            positioned = 0
            airborne = 0
            for flight in flights:
                if isinstance(flight, dict) and as_float(flight.get("latitude")) is not None:
                    positioned += 1
                    if not truthy(flight.get("on_ground"), False):
                        airborne += 1
            if flights and not positioned:
                continue        # schedule-only data: nothing with a position to plot
            area = sensor_area_km(attributes.get("bounds") or "")
            found.append({
                "entity_id": entity_id,
                "name": as_text(attributes.get("friendly_name") or entity_id, 80),
                "state": as_text(state.get("state"), 20),
                "count": positioned,
                "airborne": airborne,
                # Same figure the display prints ("area ≈50 km"), not the box diagonal.
                "area": f"≈{round(area)} km" if area else "",
            })
        # Aircraft-in-area sensors first, then anything else by name.
        found.sort(key=lambda item: (0 if "in_area" in item["entity_id"] else 1, item["entity_id"]))
        return self.send_json({"sensors": found, "default_entity": default_entity()})


def main() -> None:
    print(f"kiosk-flight: serving on port {PORT} "
          f"(Supervisor token: {'yes' if SUPERVISOR_TOKEN else 'no'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()