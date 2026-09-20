# Home Assistant Add-on — Implementation Guide (handoff from the Kiosk projects)

**Audience:** the next AI agent building a new kiosk-style HA add-on in this repo.
**Scope:** *only* how to build the add-on — file skeleton, settings design, authentication, the
proxy feature, local testing and shipping conventions. It deliberately says nothing about what
data to display or where to get it (that is this project's own session).

Everything below is copied from working code in the three sibling repos, which are the reference
implementations. Read them when in doubt:

| Repo | Port | What it demonstrates |
|---|---|---|
| `~/Documents/GitHub/HA-Kiosk-Navigation` | 8099 | per-dashboard settings, shared HERE API key in options, full proxy of an SDK + tiles + JSON APIs, unsaved-form preview |
| `~/Documents/GitHub/HA-Kiosk-News` | 8098 | multi-source list settings, server-side fetch of external content, per-item image proxy + a user-facing proxy toggle, shuffle/swipe client features |
| `~/Documents/GitHub/HA-Kiosk-Canvas` | 8097 | reading Home Assistant entities server-side via the Supervisor API (with the `homeassistant_api` permission), Pillow rendering |

Suggested port for this project: **8096**.

---

## 1. Repo skeleton

An add-on is a directory with these files. Nothing else is required.

```
app.py                 # single-file stdlib-only server: config store, admin UI, display page, APIs
web/display.html       # the kiosk page; per-display config is injected into <head> at serve time
run.sh                 # container entrypoint (execs app.py)
config.yaml            # add-on manifest: slug, ports, ingress, panel, options/schema, version
Dockerfile             # io.hass.base + python3
repository.yaml        # makes the repo installable as a local add-on repository
DOCS.md                # human-facing docs (optional but conventional here)
```

**`run.sh`** (copy verbatim):

```bash
#!/usr/bin/with-contenv bashio
set -euo pipefail
exec python3 /app/app.py
```

**`Dockerfile`** — the version label MUST be bumped in lockstep with `config.yaml` (see §8):

```dockerfile
FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.1.0" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64|armv7|armhf|i386"

RUN apk add --no-cache python3

COPY run.sh /run.sh
COPY app.py /app/app.py
COPY web /app/web
RUN chmod a+x /run.sh

CMD ["/run.sh"]
```

**`config.yaml`** — kiosk template (full-screen only; drop the ports/panel lines you don't need):

```yaml
name: Kiosk <Thing> Displays
version: 0.1.0
slug: kiosk_<thing>_displays
description: Create <thing> displays for Home Assistant dashboards and kiosks.
url: https://github.com/example/kiosk-<thing>-displays
arch:
  - aarch64
  - amd64
  - armv7
  - armhf
  - i386
startup: application
init: false
ingress: true
ingress_port: 8096
panel_icon: mdi:airplane
panel_title: Kiosk Flight
ports:
  8096/tcp: 8096
ports_description:
  8096/tcp: Direct access for an iframe when Home Assistant ingress is unavailable
options: {}
schema: {}
backup: hot
```

Add `homeassistant_api: true` **only** if you read Home Assistant entities (§4.4) — it grants the
Supervisor token access to `/core/api`. It is a permission, not a setting, and needs `schema: {}`
to stay valid.

**`repository.yaml`**:

```yaml
name: Kiosk <Thing> Displays
url: https://github.com/example/kiosk-<thing>-displays
maintainer: Local installation
```

### Runtime model you must design around

| Path | Meaning |
|---|---|
| `/data/` | persistent, survives restarts and updates — put your JSON store **and** your own token file here |
| `/app/` | your code, baked into the image, replaced on every update |
| `/data/options.json` | **Home Assistant-owned.** Written by HA from the add-on Configuration tab on *every* restart. Never store generated data here. |
| `OPTIONS_FILE` read | how you read user-set options (API keys etc.) |

`app.py` resolves paths as module constants so a local test harness can rebind them (§7):

```python
DATA_FILE = Path("/data/dashboards.json")
OPTIONS_FILE = Path("/data/options.json")
DISPLAY_FILE = Path("/app/web/display.html")
PORT = 8096
```

---

## 2. Server shape

A single `ThreadingHTTPServer` + `BaseHTTPRequestHandler`. Routes are matched with `re.fullmatch`
on `urlparse(self.path).path.rstrip("/")`. Keep these conventions:

```python
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print(fmt % args, flush=True)   # keep: logs go to the add-on log

    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def send_html(self, value, status=200):
        data = value.encode()
        # no-store is deliberate: a cached display page is exactly what keeps a
        # kiosk stuck on old, broken client code after an update.
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
```

Routes used across the three repos:

| Route | Purpose |
|---|---|
| `GET /` | embedded admin UI (create/edit/delete displays, copy links) |
| `GET /api/displays` | list configs as JSON (used by the admin page) |
| `POST /api/displays`, `PUT /api/displays/<id>`, `DELETE /api/displays/<id>` | CRUD |
| `GET /display/<id>` | the kiosk page with that display's config injected |
| `GET /api/preview/<id>` *or* `POST /api/preview` | render a preview into an iframe (see note) |
| `GET /health` | cheap readiness probe, no auth |

Notes: the admin page is a Python string constant (`admin_page()`), with `__ACCESS_TOKEN__`
substituted at serve time. Pages are served with `Cache-Control: no-store`.

**Two API-method pitfalls we hit:** the admin JS calls the preview endpoint with plain
`fetch(url)` → **GET**. Route it in `do_GET`, not only `do_POST` (the POST-only variant silently
404s with `{"error":"Not found"}` inside the preview iframe). And a POST route must be matched
*before* the generic `if path != "/api/displays": 404` branch.

---

## 3. Authentication — the three channels

There are three distinct trust relationships. Get all three right or the kiosk works for you and
fails for everyone else.

### 3.1 Ingress (Home Assistant already authenticated)

`ingress: true` + `ingress_port: 8096` makes HA proxy the add-on's UI into the HA frontend and
into iframes (Webpage card, `panel_icon` sidebar entry). Those requests arrive with HA-proxy
headers and need no token of yours:

```python
def is_ingress(self) -> bool:
    return bool(self.headers.get("X-Hassio-Ingress")
                or self.headers.get("X-Forwarded-For")
                or self.headers.get("X-Forwarded-Host"))
```

**Under ingress, build links RELATIVE.** The ingress path contains a per-session token
(`/api/hassio_ingress/<token>/…`); an absolute URL copied out of one session 401s for every other
browser and kiosk:

```js
const base = location.pathname.replace(/\/$/, '');
const displayLink = p => AUTH ? withAuth(`${base}/${p.replace(/^\//,'')}`)
                              : `${base}/${p.replace(/^\//,'')}`;
```

### 3.2 Direct LAN access (kiosks, automation-captured images, iframes without a HA session)

Publish a port (`ports: 8096/tcp`) and authenticate with a **long-lived shared token you own**.

```python
def access_token() -> str:
    """Shared access token for direct (non-ingress) connections.

    Lives in the app's OWN file (/data/access_token), NOT options.json: Home
    Assistant rewrites options.json from the add-on configuration on every
    restart, which would regenerate the token and break every saved kiosk link.
    A stable random token is created once on first read.
    """
    token_file = OPTIONS_FILE.parent / "access_token"
    try:
        token = token_file.read_text().strip()
        if token: return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = token_file.with_suffix(".tmp")
        temporary.write_text(token + "\n")
        temporary.replace(token_file)          # atomic write
    except OSError:
        return ""
    return token


def authorized(self) -> bool:
    if self.is_ingress(): return True
    expected = access_token()
    if not expected: return True    # no token file (dev box): fail OPEN, don't lock yourself out
    supplied = parse_qs(urlparse(self.path).query).get("auth", [""])[0] \
               or self.headers.get("X-Access-Token", "")
    return secrets.compare_digest(supplied, expected)
```

Rules that matter:

* Accept the token from **`?auth=` query or `X-Access-Token` header** — query for anything the
  browser loads directly (iframe `src`, `<img>`, shared links), header for scripts/curl.
* Compare with `secrets.compare_digest`, never `==`.
* The admin page gets the token injected as `__ACCESS_TOKEN__` and offers two links per display:
  **ingress** (relative, no token) and **direct** (absolute `://host:8096/...?auth=…`), plus a copy
  button. The direct link must be built from `location.hostname` and the **fixed port constant** —
  never from `location.origin`, which under ingress would wrap the ingress path and 401:

```js
const DIRECT_PORT = '8096';
const ACCESS_TOKEN = '__ACCESS_TOKEN__';
const directLink = p => ACCESS_TOKEN
  ? `${location.protocol}//${location.hostname}:${DIRECT_PORT}${p}?auth=${encodeURIComponent(ACCESS_TOKEN)}`
  : '';
```

### 3.3 Propagating auth to sub-resources (the easy thing to forget)

A page loaded as `/display/x?auth=TOM` does **not** carry `TOM` into the fetches, images,
stylesheet or script URLs it triggers. Two consequences, both of which bit us:

1. **Inject the token into the page config** so client code can append it:

```python
config["authToken"] = access_token()        # inside build_config()
```

```js
const AUTH = new URLSearchParams(location.search).get('auth') || '';   // from the URL…
const withAuth = p => p + (p.includes('?') ? '&' : '?') + 'auth=' + encodeURIComponent(AUTH);
// …or from the injected config, for client code that builds URLs itself:
const hereProxy = path => `/api/proxy?url=${encodeURIComponent(path)}&auth=${encodeURIComponent(getConfig().authToken || '')}`;
```

2. **Every admin-page fetch needs `withAuth`** or the page silently shows 401s on the direct URL
   while working fine through ingress. Apply it centrally in the one `request()` helper *and* to
   the preview `fetch` — not per call site.

### 3.4 Reading Home Assistant entities (Supervisor API) — optional

If the display needs live HA state (rather than external data), do **not** ask the kiosk for a
long-lived user token. Add `homeassistant_api: true` to `config.yaml` and read through the
Supervisor proxy with the automatically-provided token (pattern from HA-Kiosk-Canvas):

```python
import os
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()
SUPERVISOR_API = "http://supervisor/core/api"

request = Request(f"{SUPERVISOR_API}/states",
                  headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                           "Content-Type": "application/json"})
```

Cache the state map with a short TTL (Canvas uses a lock + TTL cache) so one page render makes one
call instead of one per element. Entity attributes live under `state["attributes"]`; a sensor can
carry a whole list of records there, so read attributes rather than the state string when you need
structured data. Raise a clear error when `SUPERVISOR_TOKEN` is empty (i.e. you're not running
under HA) instead of returning empty data.

---

## 4. Serving the display page (config injection)

`web/display.html` is static; per-display config is injected just before `</head>`:

```python
def render_display(self, config: dict) -> str:
    injected = json.dumps(config).replace("<", "\\u003c")     # never let data close the <script>
    flags = f"<script>window.KIOSK_FLIGHT_CONFIG={injected};</script>"
    return DISPLAY_FILE.read_text().replace("</head>", flags + "</head>", 1)
```

The page reads `window.KIOSK_FLIGHT_CONFIG` and must degrade gracefully (every option with a
default: `const LAYOUT = ['top','left'].includes(cfg.imagePosition) ? cfg.imagePosition : 'full'`).

Client-side features that carried over well and cost little: a **progress bar** that restarts per
item (`animation: grow <n>s linear` re-triggered by `void el.offsetWidth`), **shuffle** (Fisher–Yates
on a copy in the browser, server order untouched), **swipe navigation** (pointer events —
`pointerdown`/`pointerup`, ≥60 px horizontal and mostly-horizontal; clear any pending auto-advance
timer when navigating manually), and a periodic `location.reload()` for fresh server data.

**Keep the client JS conservative.** Kiosk WebViews are old: an unsupported operator kills the
*entire* script, not just its line. Observed breakers in this codebase: `??` and `String.matchAll`.
Avoid optional chaining too if you can. Test on the actual device.

---

## 5. Settings design

Two layers, and it matters which goes where:

### 5.1 Add-on-level settings → `config.yaml` `options` + `schema`

For values the whole add-on needs once (upstream API keys, defaults). These render in HA's
Configuration tab and land in `/data/options.json`:

```yaml
options:
  here_api_key: ""
schema:
  here_api_key: password
```

Read them defensively — the file may be missing or malformed:

```python
def api_key() -> str:
    try:
        return str(json.loads(OPTIONS_FILE.read_text()).get("here_api_key", "")).strip()
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
```

### 5.2 Per-display settings → your own JSON store under `/data/`

`/data/displays.json` (list of dicts) with atomic writes:

```python
def save_displays(items):
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, indent=2) + "\n")
    temporary.replace(DATA_FILE)          # never leave a half-written store
```

Validate and normalise every write through one function (`DEFAULTS` + `clean_display(payload,
existing=None)`), so the store can never hold a config the renderer can't handle:

* required fields raise `ValueError` with a message the admin UI can show verbatim;
* the `id` is derived from the name (`re.sub(r"[^a-z0-9-]+","-",raw).strip("-")[:48]`), stable on
  edit (take it from `existing`), and uniqueness-checked on create;
* every other field falls back to `DEFAULTS[key]`, so **adding a new setting later needs no
  migration** — old configs simply get the default;
* booleans are stored as the strings `"true"`/`"false"` (they travel through
  `FormData` → JSON → injected JS, and `String(v) !== 'false'` is the tolerant read).

If a display has variants (full screen vs card — Navigation does, News does), options are stored
as `<variant><Key>` (`fullTitle`, `compactTitle`) and resolved on serve:

```python
config = {**DEFAULTS, **display, "apiKey": api_key()}
for key, default in DEFAULTS.items():
    field = variant + key[0].upper() + key[1:]
    config[key] = display.get(field, display.get(key, default))
```

**Pitfall (real bug):** genuinely shared settings (proxy on/off, marker style) must be excluded
from that per-variant loop *and* from the per-variant write in `clean_*`, or the variant default
silently overrides the user's choice:

```python
if key in SHARED_KEYS: continue          # e.g. proxyTraffic: never override it
```

**A full-screen-only display (this project) needs none of this** — keep one flat settings set. Add
variants only if a compact card is actually requested.

### 5.3 The admin UI (embedded, no build step)

Same shape in all three repos: a dark page with a display list and a modal editor. Reuse these
patterns rather than inventing:

* **One modal, `openModal(display|null)`** — `resetForm()`, fill fields via
  `field(name, value)` (`f.elements[name]` guarded for missing fields), set the title, unhide.
* **Hidden input + visible picker trick:** for controls that aren't native form fields (icon
  grids, corner selectors), keep `<input type="hidden" name="x">` as the source of truth and have
  the visible widget write into it; sync *from* config when opening for edit. If you use a
  checkbox, sync `checkbox.checked` from the stored value in `openModal` (and write the hidden
  input in its `change` handler) — otherwise reopening shows a stale state.
* **Two-tap delete.** `confirm()` is silently blocked inside HA's sandboxed iframe: first tap arms
  (`Really delete?`, 3 s timeout to disarm), second tap performs the DELETE.
* **Toast** for saved/created/deleted; **copy buttons** for the generated links with a clipboard
  fallback (`navigator.clipboard` → textarea + `execCommand('copy')`).
* **Preview button** that renders the *current, unsaved* settings: POST the form to
  `/api/preview`, get back HTML, drop it into an iframe (`srcdoc`), Escape/backdrop to close, with
  a hint line for validation errors (e.g. "set an origin and destination first").
* **Submit handler** posts `Object.fromEntries(new FormData(f))` as JSON, shows a toast, closes,
  reloads the list.

**Settings vocabulary that worked** (reuse the phrasing, it reads well on a kiosk admin page):
"Text shown: Headline only / Brief", "Image position: Full screen image, text over it / Image top,
text below / …", "Story order: Feed order / Shuffled", "Image loading: Through Home Assistant
(works on isolated VLANs) / Directly from the internet (faster, needs internet on the device)",
plus number inputs with min/max, `<input type="color">`, and `1–10` sliders with a live readout.

**Design taste (the user's):** clean and uncluttered — text floating over content, no boxes or
cards unless asked; vignette-style gradient instead of a panel behind text.

---

## 6. The proxy feature (isolated-VLAN kiosks)

**Why it exists:** the kiosk devices sit on a VLAN with **no internet**. So *all* external traffic
must be resolved by the add-on inside HA: fetch data server-side, and rewrite every external
sub-resource URL to a local endpoint. The upstream credential never reaches the device.

### 6.1 Server endpoint

Generic relay with a host allowlist (never an open proxy) and API-key injection:

```python
def proxy(self) -> None:
    url = parse_qs(urlparse(self.path).query).get("url", [""])[0]
    host = urlparse(url).netloc.lower()
    if not host.endswith((".hereapi.com", ".here.com", "here.com")) \
       or not url.lower().startswith("https://"):
        return self.send_json({"error": "Only HERE service URLs may be proxied."}, 400)
    key = api_key()
    if key:
        # OVERRIDE any existing apiKey param: proxied SDK tile URLs arrive with a
        # placeholder key, so "append only if absent" leaves the placeholder in place.
        parsed = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "apikey"]
        query.append(("apiKey", key))
        url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(query)}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (KioskTraffic/1.0)"}), timeout=25) as response:
            data = response.read(30_000_000)
            content_type = response.headers.get("Content-Type", "application/octet-stream")
    except OSError as error:
        # HTTPError is an OSError: pass the upstream status through so the display
        # can say "Upstream HTTP 401" instead of a meaningless "failed".
        status = getattr(error, "code", None)
        return self.send_json({"error": f"Upstream HTTP {status}" if status else "Upstream request failed."}, 502)
    self.send_response(200)
    self.send_header("Content-Type", content_type)
    self.send_header("Cache-Control", "public, max-age=300")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)
```

For images (News) the same idea with an image allowlist, `max-age=900`, and the token baked into
the URL server-side so the kiosk needs no auth query of its own:

```python
def proxy_image_url(url: str, token: str) -> str:
    from urllib.parse import quote
    return f"/api/image?url={quote(url, safe='')}&auth={token}"
```

### 6.2 Client side — three levels, use the lowest that works

1. **URLs your own code builds** — wrap them (SDK `<script>`/`<link>` hrefs, JSON API calls).
2. **URLs another library builds for you** — rewrite at the network boundary. This is the lesson
   from HERE v3.2: it assembles tile URLs inside minified internals (`getUri` does not exist), so
   per-provider hooking silently does nothing and tiles keep going direct. Intercept instead:

```js
if (PROXY) {
  const HERE_RE = /^https:\/\/([a-z0-9-]+\.)*(hereapi\.com|here\.com)\//i;
  const rewrite = url => typeof url === 'string' && HERE_RE.test(url) ? hereProxy(url) : url;
  // tiles load as <img>.src
  const imgDesc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  Object.defineProperty(HTMLImageElement.prototype, 'src', {
    set(value) { imgDesc.set.call(this, rewrite(value)); },
    get() { return imgDesc.get.call(this); }
  });
  // JSON calls use fetch…
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => originalFetch(rewrite(String(input)), init);
  // …and XHR (the SDK's copyright lookup uses it, not fetch)
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    return originalOpen.call(this, method, rewrite(url), ...rest);
  };
}
```

Install these hooks **before** loading the third-party SDK, and make them idempotent (guard with a
flag if the page can re-run the block).

3. **A user-facing toggle.** Expose it as a normal setting (Navigation: a "Route HERE traffic
   through Home Assistant" checkbox; News: an "Image loading" select with the two explicit
   choices). Default ON for kiosks, and keep the direct path working — it is the fallback when the
   add-on is being tested on a machine that does have internet.

### 6.3 How to prove it works

Open the real page in a browser and audit network traffic — "it renders" is not proof, because a
machine with internet hides leaks:

```js
// zero direct requests to the upstream host, and every proxied URL returns 200
(() => {
  const res = performance.getEntriesByType('resource');
  const direct = res.filter(r => /hereapi\.com$|here\.com$/.test(new URL(r.name).hostname)).length;
  const proxied = res.filter(r => new URL(r.name).pathname === '/api/proxy')
                     .map(r => ({u: new URL(r.name).searchParams.get('url')?.slice(0,60), s: r.responseStatus}));
  return JSON.stringify({direct, proxied}, null, 1);
})()
```

Target: `direct: 0`. Also assert the guards from the shell — an off-allowlist host and a `file://`
URL must both return 400.

### 6.4 Vendoring beats proxying when the asset set is small and static

Some external things have no reason to be fetched at all. A small, fixed artwork set (icons,
glyphs, a flag set) is one of them: fetch it *once, at development time*, and ship it inside the
add-on. That is strictly better than a runtime proxy — it works with no internet anywhere, it
cannot fail at 3 a.m., and it survives the admin's `srcdoc` preview (which has **no base URL**, so
an `<img src="/icons/a320.svg">` in the preview resolves against the wrong origin and 404s even
when the real display is fine).

The pattern, exactly as used by the aircraft schematics in this repo:

1. **Vendor the artwork with its licence**, untouched, under `vendor/<source>/`, plus a `README.md`
   recording the source URL, the verbatim licence terms, and *where the required attribution
   lives* in this repo. Check that requirement **before** you build on the set — "free for
   commercial use" and "free with a backlink" are different projects, and a backlink is easy to
   forget until it is the only thing standing between you and a takedown.
2. **Normalise with a build script** (`tools/build_*.py`) into a single generated file the app
   reads — here `web/aircraft_icons.json`, a name → SVG-string map:
   * strip the editor cruft (XML declaration, `width`/`height`, `<defs>` grid guides, foreign
     namespaces) but KEEP `viewBox` + `preserveAspectRatio`, so CSS controls the size;
   * rewrite every colour to `currentColor` **and put `fill="currentColor"` on the root element**:
     icons that declare no `fill` at all inherit SVG's default **black**, which is invisible on a
     dark kiosk and looks fine in a JSON diff. (This bit: half the set rendered black until the
     contact sheet was actually looked at.)
   * minify, and print the total size so a runaway asset set is visible immediately.
3. **Commit the generated file** so the image builds without anyone running the build step, and
   say in the docs that the script must be re-run when the artwork changes.
4. **Inline it into the page** rather than linking it: `render_display()` adds
   `<script>window.X_ICONS={…}</script>` beside the config. Escape only `</` (a JSON string inside
   a `<script>` ends the block on it; the `<` of every SVG tag is fine as-is). ~65 KB of paths on a
   LAN is nothing, and it is the same bytes the `srcdoc` preview needs.
5. **Map data → asset server-side**, in the app, not in the client: the client gets `icon: "a320"`
   per row and looks it up. Keep the mapping table ordered by reliability (exact code → category →
   keyword in the model name → generic fallback) and write the fallback's reasoning in a comment —
   a generic silhouette is honest, a wrong one is not.

**When to proxy instead:** anything unbounded or user-supplied (map tiles, arbitrary article
images, per-request queries). Vendoring is for a closed set that fits in the repo.

**The third option — fetch once, cache on disk, inline the bytes — is for a set that is closed in
*shape* but open in *membership*.** Airline logos are exactly that: you cannot vendor them (65
airlines today, one new charter operator tomorrow) and you do not want to proxy them per request
(a 10 KB image that never changes). So the app fetches each one **once, in a background thread**,
keeps it under `/data`, and serves it to the page as a `data:` URI:

* **never block a page render on a fetch.** The poll returns whatever is cached and asks a worker
  for the rest; the first sighting of an airline shows the fallback and the artwork appears on a
  later poll. A slow or dead upstream then costs a fallback badge, never a stalled display.
* **remember failures with two clocks**: a single miss stands that one item down for an hour, and N
  network failures stand the whole fetcher down for ~30 minutes with one log line. Otherwise an
  offline host spends every poll queuing timeouts.
* **separate "not available" from "upstream is down"**: a 404 means this item has no artwork and
  must not count towards the stand-down counter.
* **inline as `data:`** so the same page works in the `srcdoc` preview and keeps working after the
  upstream has gone away — the same reasoning as the vendored set above.
* **dedupe before sending**: one `data:` URI per distinct item per payload, keyed by the code, not
  one per row (twelve arrivals from one airline must not carry 12 copies of the same 10 KB).
* **send only what the page can show** (the airlines in this display's own aircraft list), and make
  it an explicit per-display on/off setting with an honest description of what it means.

---

## 7. Local testing & verification workflow

`/data` and `/app` don't exist on a dev machine, and the ports may collide, so test through a small
harness that rebinds the module constants (keep it in `/tmp`, never in the repo):

```python
"""Local test harness: redirect /data paths and run the server."""
import importlib.util, pathlib, json, sys
spec = importlib.util.spec_from_file_location("app", "<repo>/app.py")
app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)

tmp = pathlib.Path("/tmp/kiosk-things-test"); tmp.mkdir(exist_ok=True)
app.DATA_FILE   = tmp / "displays.json"
app.OPTIONS_FILE= tmp / "options.json"
app.DISPLAY_FILE= pathlib.Path("<repo>/web/display.html")
app.save_displays([{ "id": "sample", "name": "Sample", ... }])       # seed one display
(tmp / "options.json").write_text(json.dumps({"here_api_key": "TEST_KEY"}))
from http.server import ThreadingHTTPServer
ThreadingHTTPServer(("127.0.0.1", 8096), app.Handler).serve_forever()
```

Harness gotchas, all of which wasted a cycle at least once:

* **The Python module loads once.** Restart the server after *every* `app.py` edit or you test
  stale code (the admin page is a string constant built at import time).
* `DISPLAY_FILE` is read per request, so `web/display.html` edits need only a page reload.
* A harness that re-seeds the JSON store on startup will wipe edits you made through the UI in the
  previous run — re-apply them, or point the harness at a second file.
* The token file appears at `<tmp>/access_token` on first authenticated request; read it for curl
  (`?auth=$(cat …/access_token)`).

Verification commands that actually caught bugs here:

```bash
# 1. every inline <script> in a served page must parse (catches a patched-in syntax error)
python3 - <<'EOF'
import re, subprocess
html = open('/tmp/display.html').read()
for i, m in enumerate(re.findall(r'<script>(.*?)</script>', html, re.S)):
    open(f'/tmp/js_{i}.js','w').write(m)
    r = subprocess.run(['node','--check',f'/tmp/js_{i}.js'], capture_output=True, text=True)
    print(i, 'OK' if r.returncode==0 else r.stderr[:400])
EOF

# 2. sanity of the served config
curl -s "http://127.0.0.1:8096/display/sample?auth=$T" -o /tmp/display.html
# 3. guards
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8096/api/proxy?url=https%3A%2F%2Fevil.example.com%2Fx&auth=$T"   # 400
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8096/api/proxy?url=file%3A%2F%2F%2Fetc%2Fpasswd&auth=$T"          # 400
```

**Then look at the rendered page.** For this user, claims about UI behaviour must be backed by the
actually rendered pixels (a screenshot), never an HTML/DOM dump — that is a standing expectation,
and DOM-only checks have already produced a wrong "it works". The browser tools can also drive
clicks/typing and read `performance.getEntriesByType('resource')` for the proxy audit above.

Two traps when reading files through tooling here:

* Tool output can **mask** strings that look like credentials (`params.get('apiKey')` came back as
  `params...ey'` / `***`). Never paste a masked string back into a `patch` — verify ground truth
  byte-wise first (`print([chr(b) for b in seg])`), or you corrupt the file.
* A file re-read after a partial (`offset`/`limit`) read is a partial view; re-read before
  rewriting it wholesale.

---

## 8. Shipping conventions

* **Version bump:** on *every* change, bump the last number in **two** places — `version:` in
  `config.yaml` and `io.hass.version="…"` in the `Dockerfile`. These are the shipped versions the
  user expects (e.g. `0.4.1 → 0.4.2`).
* `backup: hot` so the add-on participates in HA backups; the `/data` store rides along.
* Keep `/data/options.json` untouched (HA-owned) and never write generated state into it.
* Don't put secrets in the repo: keys live in the add-on options (`/data/options.json`) or in
  `/data` at runtime. `api_key()`/`access_token()` are the only readers.
* Commit messages in this project read like `Add proxyTraffic option: route HERE SDK, tiles,
  routing, geocoding through the add-on`.
* Port allocation so far: **8099** Navigation, **8098** News, **8097** Canvas → take **8096**.

---

## 9. Pitfall checklist (each one cost real time)

1. Access token stored in `options.json` → regenerated on every HA restart → every saved kiosk link
   breaks. Keep it in `/data/access_token`.
2. Absolute display links under ingress → per-session path → 401 for everyone else. Build links
   relative; build the *direct* link from `location.hostname` + the fixed port constant.
3. Sub-resources lose `?auth` → 401 on the direct URL while working under ingress. Inject the token
   into the page config and apply `withAuth` centrally in the API helper.
4. Preview endpoint routed in `do_POST` only while the client uses GET → "Not found" inside the
   iframe.
5. A per-variant default overriding a shared setting (`proxyTraffic`) — exclude shared keys from the
   variant loop in both read and write paths.
6. Upstream `HTTPError` swallowed as a generic 502, so a bad key looked like a broken network.
   Surface the status.
7. Proxying appended the key instead of overriding it → the SDK's placeholder `apikey=proxied` won.
8. Per-provider/`getUri` hooking of a minified third-party SDK does nothing — rewrite at the
   network boundary (`img.src` + `fetch` + `XHR.open`) instead.
9. `confirm()` is blocked in the HA ingress iframe — use two-tap delete.
10. Missing `Cache-Control: no-store` on the display page → kiosks stay on stale client code.
11. Unsupported modern JS (`??`, `matchAll`) in the display script → the whole script dies on old
    kiosk WebViews.
12. Version bumped in only one of `config.yaml` / `Dockerfile`.
13. Testing against a stale server process because `app.py` was edited without a restart.