"""Kiosk Flight Displays — a Home Assistant app showing the aircraft flying over the house.

Data comes from a Home Assistant sensor that carries a ``flights`` attribute list
(the Flightradar24 HACS integration's ``sensor.flightradar24_current_in_area``),
read through the Supervisor API. Everything is resolved inside Home Assistant, so
a kiosk on a VLAN with no internet needs nothing but this app: no map tiles, no
SDK, no external API, no key on the device.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
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
    "title": "",
    "accent": "#7dd3fc",
    "refreshInterval": "20",
}
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
            "type": as_text(flight.get("aircraft_model") or flight.get("aircraft_code"), 40),
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

    accent = as_text(combined.get("accent"), 16)
    combined["accent"] = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent or "") else DEFAULTS["accent"]
    combined["title"] = as_text(combined.get("title"), 60)
    for key in ("hideOnGround", "showRoute", "showType", "showSpeed", "showDistance",
                "showTrails", "showRings", "showSweep"):
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


def render_display(display: dict, data: dict = None) -> str:
    config = build_config(display)
    injected = json.dumps(config).replace("<", "\\u003c")
    flags = f"<script>window.KIOSK_FLIGHT_CONFIG={injected};</script>"
    if data is not None:
        flags += ("<script>window.KIOSK_FLIGHT_DATA="
                  + json.dumps(data).replace("<", "\\u003c") + ";</script>")
    return DISPLAY_FILE.read_text().replace("</head>", flags + "</head>", 1)


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
</style></head><body>
<h1>Kiosk Flight Displays</h1>
<p>Full-screen displays of the aircraft flying over your house. Data comes from the
<strong>Flightradar24</strong> integration you have running in Home Assistant (Settings → Devices &amp; services)
and is read server-side, so a kiosk on a network with no internet needs nothing else.</p>
<section><h2>New display</h2><button id="new-display">＋ Add a full-screen flight display</button></section>
<section><h2>Your displays</h2><p class="sensor-summary" id="sensor-summary">Checking the sensor…</p>
<div id="list">Loading…</div></section>
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
    oninput="document.querySelector('#max-out').textContent=this.value"></div></label>
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
<h3>Where it is centred</h3>
<label>Centre<select name="centreMode" id="centre-mode">
  <option value="home">Home — the zone.home location in Home Assistant</option>
  <option value="custom">Custom coordinates</option></select></label>
<label>Latitude<input name="latitude" placeholder="e.g. 40.7128"></label>
<label>Longitude<input name="longitude" placeholder="e.g. -74.0060"></label>
<label>Accent colour<input name="accent" type="color" value="#7dd3fc"></label>
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
  ? location.protocol+'//'+location.hostname+':'+DIRECT_PORT+route(p)+'?auth='+encodeURIComponent(ACCESS_TOKEN)
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
function resetForm(){
  f.reset();field('edit-id','');modalTitle.textContent='New display';hintEl.textContent='';
  document.querySelector('#max-out').textContent=f.elements['maxFlights'].value;
  for(var i=0;i<f.elements.length;i++){var el=f.elements[i];
    if(el.getAttribute&&el.getAttribute('data-flag')!==null&&el.type==='checkbox')el.checked=true}
  field('accent','#7dd3fc');field('maxFlights','6');field('rangeKm','0');field('refreshInterval','20');
  field('sortBy','nearest');field('units','metric');field('centreMode','home');field('showType','false');
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
  document.querySelector('#max-out').textContent=f.elements['maxFlights'].value;
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
loadSensors();load();
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
            area = sensor_bounds_km(attributes.get("bounds") or "")
            found.append({
                "entity_id": entity_id,
                "name": as_text(attributes.get("friendly_name") or entity_id, 80),
                "state": as_text(state.get("state"), 20),
                "count": positioned,
                "airborne": airborne,
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