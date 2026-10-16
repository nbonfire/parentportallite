"""Synchronous client for the KarrosTech *Edulog Parent Portal Lite* gateway.

Product note: endpoints/credentials below are for the **Lite** companion app
only (gateway "parentportallite", Keycloak realm "Edulog", client
"parent-portal-lite"). This is a one-to-one port of the standalone
`bus_client.py`; see FLOW.md for the verified protocol notes.

Everything here is synchronous — the integration calls it from the event loop
via `hass.async_add_executor_job()` / `asyncio.to_thread()`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Callable

import requests

from .const import (
    API,
    APP_TYPE,
    BUS_FOLLOW,
    CLIENT_ID,
    CLIENT_SECRET,
    FULL_ACCESS,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_TOKEN_URL,
    REGCODE_SUB,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__package__)

TIMEOUT = 30


class EdulogError(Exception):
    """Base error for the Edulog Parent Portal Lite client."""


class EdulogAuthError(EdulogError):
    """Login/refresh failed with the stored credentials."""


class EdulogSessionExpired(EdulogError):
    """The gateway rejected the JWT (HTTP 401) — refresh and retry once."""


class EdulogApiError(EdulogError):
    """Unexpected HTTP or parse failure."""


def _decode_claims(token: str) -> dict[str, Any]:
    """Best-effort JWT payload decode (no signature verification)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:  # noqa: BLE001
        return {}


def _http(
    method: str, url: str, headers: dict[str, str], body: Any = None
) -> tuple[int, Any, str | None]:
    """Thin wrapper: (status, json-or-None, raw-text-on-failure)."""
    try:
        r = requests.request(method, url, headers=headers, json=body, timeout=TIMEOUT)
    except requests.RequestException as ex:
        raise EdulogApiError(f"{method} {url}: {ex}") from ex
    try:
        return r.status_code, r.json(), None
    except ValueError:
        return r.status_code, None, r.text[:400]


class EdulogClient:
    """Keeps the access token in memory and knows how to (re-)establish it.

    Auth mode mirrors the app: Keycloak password grant first (env `prod`
    reports `authMode=keycloak`), legacy gateway `/api/v1/signin` fallback
    for non-migrated accounts. The legacy path always 401s for migrated
    accounts, and vice versa — hence the two-mode handling.
    """

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.mode: str | None = None  # "keycloak" | "legacy"
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.person_id: str | None = None
        self.expires_in: int = 1800
        self.obtained_at: float = 0.0

    # ------------------------------------------------------------------ #
    # token management                                                   #
    # ------------------------------------------------------------------ #
    @property
    def _near_expiry(self) -> bool:
        return time.time() > self.obtained_at + max(self.expires_in - 180, 30)

    def login(self) -> None:
        """Full (re-)login: Keycloak first, legacy fallback.

        Raises EdulogAuthError when neither mode accepts the credentials.
        """
        try:
            self._keycloak_login()
            return
        except EdulogAuthError:
            _LOGGER.info("Keycloak login failed; trying legacy /api/v1/signin")
        self._legacy_login()

    def ensure_token(self) -> str:
        """Return a valid access token, refreshing or re-logging in as needed."""
        if self.access_token and not self._near_expiry:
            return self.access_token
        if self.access_token or self.refresh_token:
            try:
                self._refresh()
                return self.access_token
            except EdulogAuthError:
                pass  # dead refresh token — fall through to full login
        self.login()
        return self.access_token

    def with_session_retry(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        """Run fn; on a dead JWT (401) re-authenticate once and retry."""
        try:
            return fn(*args, **kwargs)
        except EdulogSessionExpired as ex:
            _LOGGER.warning("Edulog session rejected (%s); re-authenticating", ex)
            self.login()
            return fn(*args, **kwargs)

    # -- keycloak --------------------------------------------------------
    def _keycloak_login(self) -> None:
        r = requests.post(
            KEYCLOAK_TOKEN_URL,
            data={
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
                "grant_type": "password",
                "username": self.email,
                "password": self.password,
            },
            timeout=TIMEOUT,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not data.get("access_token"):
            raise EdulogAuthError(
                f"keycloak login HTTP {r.status_code}: {str(data)[:200]}"
            )
        claims = _decode_claims(data["access_token"])
        self.mode = "keycloak"
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token")
        self.person_id = claims.get("sub")
        self.expires_in = int(data.get("expires_in", 1800))
        self.obtained_at = time.time()

    # -- legacy gateway signin (kept as fallback / for non-migrated accts)
    def _legacy_login(self) -> None:
        body = {
            "email": self.email,
            "password": self.password,
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "grantType": "resource_owner",
            "scope": APP_TYPE,
            "accountType": APP_TYPE,
            "authTokenType": FULL_ACCESS,
            "isAddNew": False,
            "deviceUUID": "",
            "isLoginWithOfflineToken": False,
        }
        # NOTE: deliberately not routed through _request — /signin returns a
        # 401 for migrated accounts even with *correct* credentials, and that
        # must not be mistaken for a dead session.
        status, data, raw = _http("POST", f"{API}/api/v1/signin", self._headers(None), body)
        if status != 200 or not (data or {}).get("accessToken"):
            raise EdulogAuthError(f"legacy login HTTP {status}: {str(data or raw)[:200]}")
        self.mode = "legacy"
        self.access_token = data["accessToken"]
        self.refresh_token = data.get("refreshToken")
        self.person_id = data.get("personId")
        self.expires_in = int(data.get("expiresIn", 1800))
        self.obtained_at = time.time()

    # -- refresh ---------------------------------------------------------
    def _refresh(self) -> None:
        if self.mode == "keycloak":
            self._keycloak_refresh()
        else:
            self._legacy_refresh()

    def _keycloak_refresh(self) -> None:
        r = requests.post(
            KEYCLOAK_TOKEN_URL,
            data={
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=TIMEOUT,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not data.get("access_token"):
            raise EdulogAuthError(
                f"keycloak refresh HTTP {r.status_code}: {str(data)[:200]}"
            )
        claims = _decode_claims(data["access_token"])
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.person_id = claims.get("sub", self.person_id)
        self.expires_in = int(data.get("expires_in", 1800))
        self.obtained_at = time.time()

    def _legacy_refresh(self) -> None:
        body = {
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "grantType": "refresh_token",
            "refreshToken": self.refresh_token,
            "scope": APP_TYPE,
            "accountType": APP_TYPE,
            "authTokenType": FULL_ACCESS,
            "email": self.email,
            "password": "",
            "deviceUUID": "",
            "isAddNew": False,
            "isLoginWithOfflineToken": False,
        }
        # Same caveat as _legacy_login: use the raw path, no session mapping.
        status, data, raw = _http("POST", f"{API}/api/v1/signin", self._headers(None), body)
        if status != 200 or not (data or {}).get("accessToken"):
            raise EdulogAuthError(f"legacy refresh HTTP {status}: {str(data or raw)[:200]}")
        self.mode = "legacy"
        self.access_token = data["accessToken"]
        self.refresh_token = data.get("refreshToken", self.refresh_token)
        self.person_id = data.get("personId", self.person_id)
        self.expires_in = int(data.get("expiresIn", self.expires_in))
        self.obtained_at = time.time()

    # ------------------------------------------------------------------ #
    # data endpoints (the two the map screen uses)                       #
    # ------------------------------------------------------------------ #
    def _headers(self, bearer: str | None) -> dict[str, str]:
        """Mirror of the app's AuthorizationInterceptor: ApiKey + clientSecret
        on every app-gateway request; Bearer JWT once signed in."""
        h = {
            "ApiKey": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "User-Agent": USER_AGENT,
        }
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        return h

    def _request(
        self, method: str, url: str, bearer: str | None, body: Any = None
    ) -> tuple[int, Any, str | None]:
        """Data-endpoint call. A 401 *while holding a token* means the JWT is
        dead → EdulogSessionExpired (caller retries after re-login)."""
        status, data, raw = _http(method, url, self._headers(bearer), body)
        if status == 401 and self.access_token is not None:
            msg = str(data or raw)[:200] if (data or raw) else "no body"
            raise EdulogSessionExpired(msg)
        return status, data, raw

    def _dsl_search(self, access_type: str) -> list[dict[str, Any]]:
        """POST /api/v2/pp/accessRequests/search — the app's exact DslQuery
        body. `includes`/`excludes` + `size=10000` are load-bearing: wrong
        select keys make the gateway return an empty result silently."""
        url = f"{API}/api/v2/pp/accessRequests/search"
        body = {
            "select": {"includes": ["*"], "excludes": []},
            "query": {"accept": {"userId": self.email, "type": access_type}},
            "page": 0,
            "size": 10000,
        }

        def _do() -> list[dict[str, Any]]:
            token = self.ensure_token()
            status, data, raw = self._request("POST", url, token, body)
            if status != 200:
                raise EdulogApiError(
                    f"accessRequests/search HTTP {status}: {str(data or raw)[:200]}"
                )
            return (data or {}).get("result") or []

        return self.with_session_retry(_do)

    def district_list(self) -> list[dict[str, Any]]:
        """One row per subscribed district (tenant).

        Fields used: tenantId, tenantName, tenantDisplayName, registrationCode,
        status (Active/Revoked/Expired), expirationDate.
        """
        return self._dsl_search(REGCODE_SUB)

    def followed_buses(self, tenant: str) -> list[dict[str, Any]]:
        """Followed-bus rows for one district. `tenant` may be the tenant
        UUID or the registration code the app UI shows."""
        rows = self._dsl_search(BUS_FOLLOW)
        out: list[dict[str, Any]] = []
        for r in rows:
            vid = r.get("vehicleId")
            if not vid:
                continue
            if str(r.get("tenantId")) != str(tenant) and str(
                r.get("registrationCode")
            ) != str(tenant):
                continue
            out.append(
                {
                    "vehicleId": str(vid),
                    "name": r.get("vehicleName") or r.get("busNumber") or f"Bus {str(vid)[:8]}",
                    "routeCode": r.get("routeCode"),
                }
            )
        return out

    def live_events(self, vehicle_ids: list[str]) -> list[dict[str, Any]]:
        """POST /api/v1.0/vehicleliveevents/search — the map's batch call:
        one request, coordinates for all followed buses.

        Buses with no *recent* GPS fix come back with a stale
        `lastUpdateTime` (epoch millis) — staleness is decided by the
        coordinator, not here.
        """
        if not vehicle_ids:
            return []
        url = f"{API}/api/v1.0/vehicleliveevents/search"

        def _do() -> list[dict[str, Any]]:
            token = self.ensure_token()
            status, data, raw = self._request("POST", url, token, list(vehicle_ids))
            if status != 200:
                raise EdulogApiError(
                    f"vehicleliveevents HTTP {status}: {str(data or raw)[:200]}"
                )
            return (data or {}).get("result") or []

        return self.with_session_retry(_do)
