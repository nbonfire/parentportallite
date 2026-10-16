# Repository Guidelines

## Project Overview

Reverse-engineering workbench for KarrosTech **"Edulog Parent Portal Lite"** (APK: `com.karrostech.parentportallite.android.apk`, v1.15.9). Goal: drive the app's proprietary gateway from Python to read live GPS coordinates of buses in a parent's own districts (tenants), scope-limited to their account. The APK was decompiled with JADX 1.5.1; all endpoints/headers/credentials were extracted statically (see `FLOW.md`).

## Architecture & Data Flow

Single script (`bus_client.py`) + docs. No framework, no DI, no state layer — procedural, one function per endpoint, `main()` dispatches CLI subcommands.

Data flow (mirrors the app):

```
login ──> POST /api/v1/signin (grantType=resource_owner)          ──> accessToken (JWT) cached in .bus_client_cache.json
   │
   ├─ POST /api/v2/pp/accessRequests/search                        ──> tenants (districts) subscribed, each with vehicles[]
   │
   └─ POST /api/v1.0/vehicleliveevents/search  [vehicleId, ...]    ──> {vehicleId, latitude, longitude, lastUpdateTime}[]
```

Key design points — change with care:

- **Every request carries three headers** (mirrors the OkHttp `AuthorizationInterceptor`): `ApiKey: <CLIENT_ID>`, `clientSecret: <CLIENT_SECRET>` (both *always*), and `Authorization: Bearer <JWT>` once signed in. `build_headers()` is the single place this is enforced — do not ad-hoc headers elsewhere.
- **District = tenant.** You never query all buses in a district; you query tenants your account is subscribed to (registration-code subscriptions) and each tenant carries its vehicle list. Coordinates come only per `vehicleId` batch.
- **Buses without a recent GPS fix are absent** from the live-event result, not null — treat missing as "no location".
- Token lifecycle: `load_token()` → `refresh_token()` (same `/api/v1/signin`, `grantType=refresh_token`) → login required. Cache file is written `0600` in `cache_set()`.

## Key Directories

| Path | Contents |
|---|---|
| (root) | Everything: `bus_client.py` (client), `custom_components/edulog_lite/` (HACS integration), `FLOW.md` (full reverse-engineering notes + API contract), `README.md`, `.env` (credentials, gitignored), `.bus_client_cache.json` (cached token, gitignored) |
| `.venv/` | uv-managed virtualenv (Python 3.14, `requests` only) |

## Development Commands

No build system, no package manifest, no linter — it's a standalone script.

```sh
# venv (already created via uv)
.venv/bin/python bus_client.py            # prints usage

# auth (reads .env EMAIL/PASSWORD if no args)
.venv/bin/python bus_client.py login
.venv/bin/python bus_client.py show       # cached token + personId

# data
.venv/bin/python bus_client.py district            # list subscribed tenants + vehicle counts
.venv/bin/python bus_client.py district <tenantId> # bus ids in one tenant
.venv/bin/python bus_client.py follow <vehicleId>  # one-shot lat/lng
.venv/bin/python bus_client.py stream <tenantId> 10 # poll all buses, 10s interval (Ctrl-C stops)
```

## Code Conventions & Common Patterns

- **Style:** stdlib + `requests` only. No type hints, no classes, no argparse — manual `sys.argv` dispatch in `main()`. Keep it that way; this is a CLI script, not a package.
- **Constants:** app-derived constants (gateway URL, client id/secret, auth strings) are module-level `UPPER_SNAKE` with a comment tying each value to the APK (`BuildConfig.kt`). If an endpoint/field is verified against the server, note it in `FLOW.md`.
- **HTTP:** all calls go through `api(method, url, bearer, json_body)` → `(status_code, data)`; non-JSON bodies land under `{"_raw": ...}`. Per-command error handling prints the status + JSON dump and returns `None`/`[]`.
- **Secrets:** credentials come from args > `os.environ` > `.env` (minimal built-in loader in `load_dotenv`, real env vars win). Tokens are persisted with `os.chmod(0o600)`. Never print full tokens (`cmd_show` truncates).
- **PII hygiene (repo is public):** never paste personal identifiers into repo files — emails, tenant/vehicle UUIDs, district names or registration codes, GPS coordinates. `FLOW.md` uses truncated placeholders (`4497...`, `95a9cfa2-...`, `<parent email>`); keep it that way.
- **Output:** `[*]` prefixes for progress lines, `!!` for failures (see `do_login`, `fetch_bus_list`).
- **Polling:** `stream` is a plain `while True` + `time.sleep` with `KeyboardInterrupt` catch — match that idiom for any new polling feature.

## Important Files

- `bus_client.py` — the entire client. Sections are header-commented: `Credentials & endpoints`, `HTTP layer`, `Auth`, `District (tenant) discovery`, `Bus coordinates`.
- `FLOW.md` — authoritative API contract doc: tenant model, all endpoint bodies/responses, static credentials table, legacy-vs-Keycloak auth-mode notes, server-side caveats (10k `size` cap, envconfig per-user endpoint). **Update it whenever the client's request/response shapes change.**
- `.env` — `EMAIL` / `PASSWORD`; gitignored; required only for `login`.
- `.bus_client_cache.json` — cached `token` record (access/refresh JWT, personId, email, expiresIn, obtainedAt); gitignored; delete to force re-login.
- `com.karrostech.parentportallite.android.apk` — the analysis target.

## Runtime & Tooling Preferences

- **Python 3.14** in `.venv/`, managed with **uv** (see `.venv/pyvenv.cfg`). Sole dependency: `requests` (recreate with `uv venv .venv && uv pip install requests` if lost).
- Use `.venv/bin/python` explicitly — there is no `pyproject.toml`/`make`/`package.json` to discover entry points.
- APK re-analysis tools used previously: JADX 1.5.1 headless CLI (scratch dirs `jadx*`, `src/`, `*.strings`, `*.log` are gitignored).

## Testing & QA

- No test suite, no CI, no linter. Verification is **live probing against production**.
- Standard acceptance checks (from `FLOW.md`): gateway returns `401 {"message":"Unauthorized","error":"JWT Token not found."}` on a data endpoint with no Bearer header — proves endpoint path + `ApiKey`/`clientSecret` headers correct; a valid login JWT then makes the two data endpoints return `200`.
- Safe verification without credentials: `bus_client.py` prints usage with no args and `cmd_show` reports the cached-token state without networking.
- **Operational caution:** this talks to a third-party production service. Don't share the JWT (cache file is gitignored for a reason), don't poll faster than the ~10s `stream` default — aggressive polling risks rate-limiting or account flags.
