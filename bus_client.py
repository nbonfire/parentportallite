#!/usr/bin/env python3
"""
Client for KarrosTech "Edulog Parent Portal Lite" (com.karrostech.parentportallite.android).

Reproduces the app's auth flow and reads every bus (vehicle) live coordinate
for a given district, scoped to the caller's own parent user login.

Auth (verified against production 2026-10-15):
  * The app logs in via Keycloak:
      POST https://accounts.karrostech.net/auth/realms/Edulog/protocol/openid-connect/token
      form: client_id=parent-portal-lite, client_secret=..., grant_type=password,
            username=<email>, password=<pw>          (NO scope field — "Parent Portal Lite" scope is invalid)
  * Access tokens are short-lived (30 min); refresh with the same endpoint +
      grant_type=refresh_token.
  * Legacy accounts still hit the app gateway's /api/v1/signin (kept as fallback).
  * Districts ("tenants") + their buses come from
      POST {appGateway}/api/v2/pp/accessRequests/search
      body: {"select":{"includes":["*"],"excludes":[]},"query":{"accept":{"userId":<email>,"type":...}},"page":0,"size":10000}
                     (type = REGISTRATIONCODE_SUBSCRIPTION for districts, BUS_FOLLOW_REQUEST for followed buses)
  * The app's map polls /api/v1.0/vehicleliveevents/search with a JSON array of
    vehicleIds; single lookups use GET /api/v1/pp/vehicleliveevents?vehicleId=..&tenantId=..
    (tenantId is required there).

A note on IDs: the "district" number shown in the app (e.g. 4497...) is the
registration CODE of a BUS_FOLLOW_REQUEST row; the district itself is the tenant
(tenantId, a UUID). `district`/`stream` accept either.

Usage:
    python3 bus_client.py login                     # reads EMAIL/PASSWORD from .env or env vars
    python3 bus_client.py login  <email> <password> # explicit (overrides .env)
    python3 bus_client.py district [code|tenantId]  # district subscriptions + followed buses
    python3 bus_client.py follow <vehicleId>        # one-shot coords (tenant auto-resolved)
    python3 bus_client.py stream [code|tenantId] [poll_sec]  # live coords for followed buses
    python3 bus_client.py show                      # cached token summary

Token is cached in .bus_client_cache.json next to this script.
"""

import base64
import json
import os
import sys
import time
import urllib.parse

import requests

# --- Credentials & endpoints (from APK BuildConfig.kt) -------------------------
API        = "https://parentportallitegateway-p01-prod.usw1.karrostech.net"   # app gateway (env "prod")
CLIENT_ID     = "c62ac14ec31ffdd04d98268aeb6ec3b5244b1bcf"     # ApiKey header (app gateway)
CLIENT_SECRET = "138abf2e93cf885379c77054e15673e73fe70b5117f42ca498a42c3955e1658b"

KEYCLOAK_BASE    = "https://accounts.karrostech.net"
KEYCLOAK_REALM   = "Edulog"
KEYCLOAK_TOKEN   = f"{KEYCLOAK_BASE}/auth/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
KEYCLOAK_CLIENT_ID     = "parent-portal-lite"
KEYCLOAK_CLIENT_SECRET = "ca78fc18-ad06-461d-a0fe-1af98446cad7"

APP_TYPE   = "Parent Portal Lite"     # application_type (legacy path)
FULL_ACCESS = "Full access"           # authTokenType (legacy path)
REGCODE_SUB  = "REGISTRATIONCODE_SUBSCRIPTION"   # access-request type: district subscriptions
BUS_FOLLOW   = "BUS_FOLLOW_REQUEST"              # access-request type: followed buses

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bus_client_cache.json")
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


# --- Credentials from .env / environment --------------------------------------
def load_dotenv(path=ENV_FILE):
    """Minimal .env loader (KEY=value, # comments, optional export / quotes).
    Real environment variables take precedence."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def load_credentials(email=None, password=None):
    """Resolve EMAIL/PASSWORD from args > environment. Falls back to .env, then
    raises a clear error if any is still missing."""
    load_dotenv()
    email = email or os.environ.get("EMAIL")
    password = password or os.environ.get("PASSWORD")
    if not email or not password:
        missing = ", ".join(k for k, v in (("EMAIL", email), ("PASSWORD", password)) if not v)
        src = ENV_FILE if os.path.exists(ENV_FILE) else "(none)"
        raise SystemExit(f"missing {missing}: pass as args or set EMAIL/PASSWORD in .env (looked in {src}) / env vars")
    return email, password


# --- HTTP layer ----------------------------------------------------------------
def build_headers(bearer=None):
    """Mirror of AuthorizationInterceptor for the app gateway: ApiKey + clientSecret
    on every request; Authorization: Bearer <access_token> once signed in."""
    h = {"ApiKey": CLIENT_ID, "clientSecret": CLIENT_SECRET,
         "User-Agent": "Edulog Parent Portal Lite/1.15.9 (com.karrostech.parentportallite.android; build:2160901)"}
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return h


def api(method, url, bearer=None, json_body=None):
    r = requests.request(method, url, headers=build_headers(bearer),
                         json=json_body, timeout=30)
    if r.status_code >= 400 or not r.text:
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        return r.status_code, data, r.text[:400]
    return r.status_code, r.json(), None


# --- Token cache ---------------------------------------------------------------
def cache_get():
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            return json.load(f)
    return {}


def cache_set(k, v):
    d = cache_get()
    d[k] = v
    with open(CACHE, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.chmod(CACHE, 0o600)  # tokens are secrets


# --- Auth ----------------------------------------------------------------------
def _kc_token_request(fields):
    r = requests.post(KEYCLOAK_TOKEN, data=fields, timeout=30)
    try:
        return r.status_code, r.json(), None
    except Exception:
        return r.status_code, {}, r.text[:400]


def _keycloak_login(email, password):
    """The app's actual login (env auth mode = keycloak). No scope field:
    sending scope='Parent Portal Lite' returns 400 invalid_scope."""
    fields = {"client_id": KEYCLOAK_CLIENT_ID,
              "client_secret": KEYCLOAK_CLIENT_SECRET,
              "grant_type": "password",
              "username": email,
              "password": password}
    status, data, raw = _kc_token_request(fields)
    if status != 200 or not data.get("access_token"):
        return None, (data or {"_raw": raw, "status": status})
    return data, None


def _legacy_login(email, password):
    """Fallback for non-migrated accounts: gateway /api/v1/signin (JWT)."""
    body = {
        "email": email, "password": password,
        "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET,
        "grantType": "resource_owner", "scope": APP_TYPE, "accountType": APP_TYPE,
        "authTokenType": FULL_ACCESS, "isAddNew": False,
        "deviceUUID": "", "isLoginWithOfflineToken": False,
    }
    status, data, raw = api("POST", f"{API}/api/v1/signin", json_body=body)
    if status != 200 or not data.get("accessToken"):
        return None, (data or {"_raw": raw, "status": status})
    return data, None


def _decode_claims(token):
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def do_login(email, password):
    print(f"[*] login {email!r} via Keycloak (accounts.karrostech.net)")
    kc, kc_err = _keycloak_login(email, password)
    if kc:
        claims = _decode_claims(kc["access_token"])
        roles = claims.get("resource_access", {}).get(KEYCLOAK_CLIENT_ID, {}).get("roles", [])
        print(f"    OK  personId(sub)={claims.get('sub')}  roles={roles}  expiresIn={kc.get('expires_in')}s")
        cache_set("token", {
            "mode": "keycloak",
            "accessToken": kc["access_token"],
            "refreshToken": kc.get("refresh_token"),
            "personId": claims.get("sub"),
            "email": email,
            "expiresIn": kc.get("expires_in", 1800),
            "obtainedAt": int(time.time()),
        })
    else:
        print(f"    keycloak failed: {json.dumps(kc_err)[:300]}")
        print(f"[*] falling back to legacy /api/v1/signin")
        lg, lg_err = _legacy_login(email, password)
        if not lg:
            print(f"    legacy failed: {json.dumps(lg_err)[:300]}")
            print("!! login failed (both auth modes)")
            return False
        print(f"    OK  personId={lg.get('personId')}  expiresIn={lg.get('expiresIn')}s")
        cache_set("token", {
            "mode": "legacy",
            "accessToken": lg["accessToken"],
            "refreshToken": lg.get("refreshToken"),
            "personId": lg.get("personId"),
            "email": email,
            "expiresIn": lg.get("expiresIn"),
            "obtainedAt": int(time.time()),
        })
    print(f"[*] token cached -> {CACHE}")
    return True


def _refresh_cache(token):
    cache_set("token", token)
    return token["accessToken"]


def refresh_token():
    t = cache_get().get("token")
    if not t or not t.get("refreshToken"):
        return None
    if t.get("mode") == "keycloak":
        fields = {"client_id": KEYCLOAK_CLIENT_ID,
                  "client_secret": KEYCLOAK_CLIENT_SECRET,
                  "grant_type": "refresh_token",
                  "refresh_token": t["refreshToken"]}
        status, data, raw = _kc_token_request(fields)
        if status == 200 and data.get("access_token"):
            claims = _decode_claims(data["access_token"])
            print("[*] refreshed keycloak token")
            return _refresh_cache({
                "mode": "keycloak",
                "accessToken": data["access_token"],
                "refreshToken": data.get("refresh_token", t["refreshToken"]),
                "personId": claims.get("sub", t.get("personId")),
                "email": t.get("email"),
                "expiresIn": data.get("expires_in", 1800),
                "obtainedAt": int(time.time()),
            })
        print(f"[*] keycloak refresh failed (HTTP {status}): {json.dumps(data)[:200] if data else raw}")
        return None
    body = {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET,
            "grantType": "refresh_token", "refreshToken": t["refreshToken"],
            "scope": APP_TYPE, "accountType": APP_TYPE,
            "authTokenType": FULL_ACCESS, "email": t.get("email", ""),
            "password": "", "deviceUUID": "", "isAddNew": False, "isLoginWithOfflineToken": False}
    status, data, raw = api("POST", f"{API}/api/v1/signin", json_body=body)
    if status == 200 and data.get("accessToken"):
        print("[*] refreshed legacy token")
        return _refresh_cache({
            "mode": "legacy",
            "accessToken": data["accessToken"],
            "refreshToken": data.get("refreshToken", t.get("refreshToken")),
            "personId": data.get("personId", t.get("personId")),
            "email": t.get("email"),
            "expiresIn": data.get("expiresIn", t.get("expiresIn")),
            "obtainedAt": int(time.time()),
        })
    print(f"[*] legacy refresh failed (HTTP {status})")
    return None


def get_token():
    """Return a valid bearer: cached token, refreshed if nearing expiry, else None."""
    t = cache_get().get("token")
    if not t or not t.get("accessToken"):
        return None
    ttl = t.get("expiresIn") or 1800
    if time.time() > t.get("obtainedAt", 0) + max(ttl - 120, 30):
        return refresh_token()
    return t["accessToken"]


# --- District (tenant) discovery ----------------------------------------------
def dsl_search(bearer, user_id, access_type):
    """POST /api/v2/pp/accessRequests/search — app-identical DslQueryRequest
    (verified against live app capture: select uses includes/excludes, size 10000)."""
    url = f"{API}/api/v2/pp/accessRequests/search"
    body = {"select": {"includes": ["*"], "excludes": []},
            "query": {"accept": {"userId": user_id, "type": access_type}},
            "page": 0, "size": 10000}
    status, data, raw = api("POST", url, bearer, json_body=body)
    return status, (data or {}), raw


def list_districts(bearer, user_id):
    """REGISTRATIONCODE_SUBSCRIPTION rows = one per subscribed district."""
    print(f"[*] district subscriptions (REGISTRATIONCODE_SUBSCRIPTION, userId={user_id})")
    status, data, raw = dsl_search(bearer, user_id, REGCODE_SUB)
    if status != 200:
        print(f"    HTTP {status}: {(raw or json.dumps(data))[:300]}")
        return []
    result = data.get("result", [])
    if not result:
        print("    no rows")
        return []
    for d in result:
        print(f"  tenantId={d.get('tenantId')}  {d.get('tenantDisplayName')!r}  "
              f"code={d.get('registrationCode')}  status={d.get('status')}")
    return result


def list_followed(bearer, user_id):
    """BUS_FOLLOW_REQUEST rows = followed buses (each row carries its vehicleId)."""
    status, data, raw = dsl_search(bearer, user_id, BUS_FOLLOW)
    if status != 200:
        print(f"!! followed-bus search HTTP {status}: {(raw or json.dumps(data))[:300]}")
        return []
    return data.get("result", [])


def followed_buses(bearer, user_id, tenant_id=None):
    """[(vehicleId, name, routeCode, tenantId, code)] from BUS_FOLLOW rows.
    tenant_id may be the tenant UUID or a registration code (e.g. '4497...')."""
    rows = list_followed(bearer, user_id)
    out = []
    for r in rows:
        vid = r.get("vehicleId")
        if not vid:
            continue
        if tenant_id is not None and \
           str(r.get("tenantId")) != str(tenant_id) and \
           str(r.get("registrationCode")) != str(tenant_id):
            continue
        out.append((vid, r.get("vehicleName") or r.get("busNumber"),
                    r.get("routeCode"), r.get("tenantId"), r.get("registrationCode")))
    return out


# --- Bus coordinates -----------------------------------------------------------
def fetch_bus_list(bearer, vehicle_ids):
    """POST /api/v1.0/vehicleliveevents/search -> {result:[{vehicleId,lat,lng,lastUpdateTime}...]}"""
    if not vehicle_ids:
        return []
    url = f"{API}/api/v1.0/vehicleliveevents/search"
    status, data, raw = api("POST", url, bearer, json_body=vehicle_ids)
    if status != 200:
        print(f"!! vehicleliveevents HTTP {status}: {(raw or json.dumps(data))[:300]}")
        return []
    return data.get("result", [])


def one_bus(bearer, vehicle_id, tenant_id=None):
    """GET /api/v1/pp/vehicleliveevents?vehicleId=..[&tenantId=..] -> {status:{lat,lng,...}}"""
    qs = f"?vehicleId={urllib.parse.quote(str(vehicle_id))}"
    if tenant_id:
        qs += f"&tenantId={urllib.parse.quote(str(tenant_id))}"
    status, data, raw = api("GET", f"{API}/api/v1/pp/vehicleliveevents{qs}", bearer)
    if status != 200:
        print(f"!! vehicleliveevents(single) HTTP {status}: {(raw or json.dumps(data))[:300]}")
        return None
    return data.get("status")


def fmt_coord(rec):
    if not rec:
        return "n/a"
    lat, lng = rec.get("latitude"), rec.get("longitude")
    upd = rec.get("lastUpdateTime")
    ts = time.strftime("%H:%M:%S", time.localtime(upd / 1000)) if upd else "?"
    return f"lat={lat} lng={lng}  updated={ts}"


# --- Commands -------------------------------------------------------------------
def _user_id():
    t = cache_get().get("token")
    if t and t.get("email"):
        return t["email"]
    raise SystemExit("no cached login (no email): run `login` first")

def cmd_district(bearer, tenant_id):
    uid = _user_id()
    subs = list_districts(bearer, uid)
    print(f"[*] followed buses (BUS_FOLLOW_REQUEST, userId={uid})")
    buses = followed_buses(bearer, uid, tenant_id)
    if not subs:
        print("    no rows")
    if not buses:
        print("    no followed buses")
        return
    label = f" matching {tenant_id}" if tenant_id else ""
    print(f"    {len(buses)} bus(es){label}:")
    for vid, name, route, t, code in buses:
        print(f"      {vid}  name={name!r}  code={code}  tenant={t}  route={route!r}")



def cmd_stream(poll_sec=10.0, user_id=None, tenant_id=None):
    tok = get_token()
    if not tok:
        print("no valid token. run: login")
        return
    uid = user_id or _user_id()
    veh = followed_buses(tok, uid, tenant_id)
    ids = [v[0] for v in veh]
    if not ids:
        print("no followed buses for this account (see `district`)")
        return
    names = {v[0]: v[1] for v in veh}
    print(f"streaming {len(ids)} bus(es) every {poll_sec:g}s  (Ctrl-C to stop)")
    try:
        while True:
            tok = get_token()
            if not tok:
                print("!! no valid token; run: login")
                return
            recs = fetch_bus_list(tok, ids)
            now = time.strftime("%H:%M:%S")
            print(f"--- {now} ---")
            for r in recs:
                vid = r.get("vehicleId")
                print(f"    {str(vid)[:14]:<14}  {fmt_coord(r)}  {names.get(vid, '')}")
            if not recs:
                print("    (no live events yet)")
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_follow(bearer, vehicle_id, tenant_id=None):
    if not tenant_id:
        for r in list_followed(bearer, _user_id()):
            if r.get("vehicleId") == str(vehicle_id):
                tenant_id = r.get("tenantId")
                break
    rec = one_bus(bearer, vehicle_id, tenant_id)
    if rec:
        print("vehicle:", vehicle_id)
        print(fmt_coord(rec))
    else:
        print(f"no position for vehicle {vehicle_id}")


def cmd_show():
    t = cache_get().get("token")
    if not t:
        print("no login stored")
        return
    print(f"mode       = {t.get('mode')}")
    print(f"email      = {t.get('email')}")
    print(f"personId   = {t.get('personId')}")
    print(f"obtainedAt = {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t.get('obtainedAt', 0)))}")
    print(f"expiresIn  = {t.get('expiresIn')}s")
    print(f"token      = {t.get('accessToken')[:40]}...")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return
    cmd = a[0]
    if cmd == "login":
        email = a[1] if len(a) > 1 else None
        password = a[2] if len(a) > 2 else None
        do_login(*load_credentials(email, password))
    elif cmd == "show":
        cmd_show()
    elif cmd in ("district", "follow", "stream"):
        tok = get_token()
        if not tok:
            print("no valid token. run:  python3 bus_client.py login")
            return
        if cmd == "district":
            tenant = a[1] if len(a) > 1 else None
            cmd_district(tok, tenant)
        elif cmd == "follow":
            tenant = a[2] if len(a) > 2 else None
            cmd_follow(tok, a[1], tenant)
        elif cmd == "stream":
            args = a[1:]
            tenant = None
            # first positional that is a big int (>3600) = tenant code/uuid; small = poll seconds
            if args and args[0].isdigit() and int(args[0]) > 3600:
                tenant = args[0]
                args = args[1:]
            elif args and args[0].startswith(("9", "a", "b", "c", "d", "e", "f", "0")) \
                    and len(args[0]) > 8:  # looks like a uuid/long id
                tenant = args[0]
                args = args[1:]
            poll = float(args[0]) if args else 10.0
            cmd_stream(poll_sec=poll, tenant_id=tenant)


if __name__ == "__main__":
    main()
