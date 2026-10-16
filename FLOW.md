# Edulog Parent Portal Lite — Auth Flow & Bus Coordinates

Reverse-engineered from `com.karrostech.parentportallite.android.apk` (v1.15.9,
versionCode 2160901, flavor `nonDevLegacy`). Decompiled with JADX.
Working client: **`bus_client.py`** (next to this file).
Auth verified against production on 2026-10-15 (real login OK).

## 1. What the "district ID" actually is

The app calls a district a **tenant**. A parent subscribes to district(s) with a
**registration code**. The app scopes everything to the logged-in user: you query
"my districts" (each row already lists its **vehicles/buses**), then fetch GPS per
`vehicleId`. There is no "give me every bus in district X" endpoint.

## 2. Auth: it's per-account (Keycloak, not legacy)

**The app does NOT simply POST email/password to `/api/v1/signin`.** Flow
(`LoginUseCase.mainJob`):

1. `POST https://accounts.karrostech.net/auth/realms/Edulog/protocol/openid-connect/token`
   form: `client_id=parent-portal-lite`, `client_secret=<kc secret>`,
   `grant_type=client_credentials` → client-credential JWT.
2. `GET https://envconfig.karrostech.io/envs/v2/user/{email}` with
   `client_id: parent-portal-lite`, `scope: Parent Portal Lite`,
   `Authorization: Bearer <token from step 1>`.
   → `UserEnv { appGateway, keycloakGateway, keycloakClientSecret, authMode, ... }`.
   For the `prod` env: **`authGateways.code = "keycloak"`**,
   `isKeycloakAuthenForMobile: true`.
3. Branch on auth mode:
   - **keycloak** (this account): `POST {keycloakGateway}/auth/realms/Edulog/protocol/openid-connect/token`
     form: `client_id=parent-portal-lite`, `client_secret=<per-user kc secret>`,
     `grant_type=password`, `username=<email>`, `password=<pw>`.
   - **legacy** (non-migrated accounts): `POST {appGateway}/api/v1/signin`
     JSON `LoginParams {email, password, clientId, clientSecret, grantType=resource_owner,
     scope=Parent Portal Lite, accountType=Parent Portal Lite, authTokenType=Full access,
     isAddNew=false, deviceUUID, isLoginWithOfflineToken=false}`.
4. The app then verifies the JWT has `resource_access["parent-portal-lite"].roles`
   containing `client-user` / `system-user` / `tenant-admin`.

### Keycloak login quirks (verified)
- **Do NOT send `scope=Parent Portal Lite`** in the password grant → `400 invalid_scope`.
  Omitting the field succeeds (the app's token ends up with `scope: "profile openid email"`).
- Access token TTL is **1800 s (30 min)**; refresh via the same endpoint with
  `grant_type=refresh_token`.
- Wrong-credential error: `400 invalid_grant Invalid user credentials`.
- The legacy `/api/v1/signin` path **always 401s for migrated accounts** even with
  correct credentials ("Signin failed, please try again...") — that's the classic
  trap and it's what bit the first version of this client.

### Every gateway request (app gateway)
```
ApiKey:        c62ac14ec31ffdd04d98268aeb6ec3b5244b1bcf
clientSecret:  138abf2e93cf885379c77054e15673e73fe70b5117f42ca498a42c3955e1658b
Authorization: Bearer <access_token>
```
(For `accounts.karrostech.net` + `envconfig.karrostech.io/envs/v2/user/` the app instead
sends `client_id: parent-portal-lite` + `client_secret: <kc secret>` — see
`AuthorizationInterceptor.apiAuthorizeV2List`.)

| Constant | Value |
|---|---|
| app gateway (env `prod`) | `https://parentportallitegateway-p01-prod.usw1.karrostech.net` |
| ApiKey (`CLIENT_ID`) | `c62ac14ec31ffdd04d98268aeb6ec3b5244b1bcf` |
| gateway `clientSecret` | `138abf2e93cf885379c77054e15673e73fe70b5117f42ca498a42c3955e1658b` |
| Keycloak realm base | `https://accounts.karrostech.net/auth/realms/Edulog` |
| Keycloak client id / secret | `parent-portal-lite` / `ca78fc18-ad06-461d-a0fe-1af98446cad7` |
| client-credential + password + refresh endpoints | `{kc base}/protocol/openid-connect/token` |
| env config | `https://envconfig.karrostech.io/envs/v2/user/{email}` |

## 3. Districts + buses (one search endpoint, two access types)

`POST {appGateway}/api/v2/pp/accessRequests/search` — **exact** app body (captured from
the running app via mitmproxy; `select` uses `includes`/`excludes`, not `include`/`exclude`,
and `size` is 10000, not 50 — getting these wrong returns an empty `result`):
```json
{ "select": { "includes": ["*"], "excludes": [] },
  "query":  { "accept": { "type": "REGISTRATIONCODE_SUBSCRIPTION",
                          "userId": "<login email>" } },
  "page": 0, "size": 10000 }
```
`select` keys are `includes`/`excludes` (Moshi field names) and `size=10000` — the
exact bytes the APK sends. A wrong key name on `select` makes the gateway return
`{"result": null}` / empty.
`type` values (both go through the same endpoint, same body):
- `REGISTRATIONCODE_SUBSCRIPTION` → one row per district subscription:
  `{tenantId (UUID), tenantName, tenantDisplayName, registrationCode, status
  (Active/Revoked/Expired), expirationDate, ...}`
- `BUS_FOLLOW_REQUEST` → one row per followed bus; the bus details are ON the row:
  `{vehicleId, vehicleName, busNumber, registrationCode (the "district number" the
  UI shows, e.g. 4497...), tenantId, routeCode, enable, ...}`

Response semantics (verified):
- `200` + **empty body** → user exists, zero matching rows.
- `400` wrapping `...HttpClientErrorException$Forbidden: 403 [no body]` → user not found.
- `401 {"message":"Unauthorized","error":"JWT Token not found."}` → missing/expired JWT
  (the gateway re-issues the call; refresh the token).

## 4. Bus coordinates (live GPS)

Batch (powers the map):
```
POST {appGateway}/api/v1.0/vehicleliveevents/search
Body: ["<vehicleId>", ...]
→ { serviceCode, serviceMessage, result: [
     { vehicleId, latitude, longitude, lastUpdateTime }, ... ] }
```
Single:
```
GET {appGateway}/api/v1/pp/vehicleliveevents?vehicleId=<vehicleId>&tenantId=<tenantId>
→ { status: { vehicleId, latitude, longitude, lastUpdateTime } }   (tenantId REQUIRED — HTTP 400 without it)
```
`bus_client.py follow` auto-resolves the tenant from the account's followed-bus rows.
`latitude`/`longitude` are decimal degrees; `lastUpdateTime` epoch-millis.
Buses with no recent fix are still returned, with a stale `lastUpdateTime`.

## 5. Replication (bus_client.py)

```
python3 bus_client.py login                     # EMAIL/PASSWORD from .env
python3 bus_client.py login  <email> <password> # explicit
python3 bus_client.py show                      # cached token summary (mode, personId)
python3 bus_client.py district [code|tenantId]  # district subscriptions + followed buses
python3 bus_client.py follow <vehicleId>        # one-shot lat/lng (tenant auto-resolved)
python3 bus_client.py stream [code|tenantId] [poll_sec]  # live coords for followed buses
```
Login tries Keycloak first, falls back to legacy `/signin`. Tokens + refresh logic
live in `.bus_client_cache.json` (chmod 600, git-ignored). `stream` auto-refreshes
when the 30-minute token nears expiry.

## 6. Verified state (2026-10-15)

End-to-end with the account's real credentials (`<parent email>`,
My Town Public Schools, tenant `95a9cfa2-...`):
- Keycloak password grant: **200**; roles `resource_access[parent-portal-lite] =
  ["client-user"]`.
- `accessRequests/search` + `includes`/`excludes` + `size=10000`: **4 district rows**
  (codes 4836.../1174... Revoked, 5384... Active, **4497... Active**) and
  **2 followed-bus rows** under code 4497...: `Bus A` (`d3853ef2-...`) and
  `Bus B` (`0d1798d7-...`).
- `vehicleliveevents`: **live coordinates** for both buses, updating between polls
  (Bus A moved ~40 m between two fixes ~15 s apart).

## Notes / caveats
- Keycloak access tokens live 30 min; refresh grant keeps them rolling.
- `size` must match the app's 10000 and `select` must use `includes`/`excludes`
  (the gateway silently returns an empty result for wrong select keys).
- Don't share the JWT; heavy polling may trip rate limits or account flags. This
  reverse-engineers a third-party service for your own account access only.
