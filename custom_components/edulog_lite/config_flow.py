"""Config + options flow for the Edulog Parent Portal Lite integration.

Steps: user (email/password, live login) → district (pick a subscribed
school district) → entry. One entry per (account, district); the entry
tracks every bus that account follows in the chosen district.

"District" = tenant. The app scopes everything to the logged-in parent:
districts come from their REGISTRATIONCODE_SUBSCRIPTION rows, buses from
their BUS_FOLLOW_REQUEST rows filtered to the chosen tenant.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import EdulogAuthError, EdulogClient
from .const import (
    CONF_POLL_INTERVAL,
    CONF_TENANT_CODE,
    CONF_TENANT_ID,
    CONF_TENANT_NAME,
    CONF_THRESHOLD_M,
    CONF_VEHICLE_IDS,
    CONF_WATCH_LAT,
    CONF_WATCH_LNG,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_THRESHOLD_M,
    DOMAIN,
    POLL_INTERVAL_CHOICES,
)

_LOGGER = logging.getLogger(__package__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_PASSWORD, description={"password": "New password for this account"}
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

_COORD = vol.Any("", vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)))


class EdulogConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Edulog Parent Portal Lite."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: EdulogClient | None = None
        self._districts: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # user step                                                          #
    # ------------------------------------------------------------------ #
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client = EdulogClient(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            try:
                # validate credentials against production before asking for
                # anything else — login doubles as connectivity check
                await asyncio.to_thread(client.login)
                self._districts = await asyncio.to_thread(client.district_list)
                self._client = client
            except EdulogAuthError as ex:
                _LOGGER.info("Edulog login failed: %s", ex)
                errors[CONF_PASSWORD] = "invalid_auth"
            except Exception as ex:  # noqa: BLE001
                _LOGGER.exception("Edulog config flow error")
                errors["base"] = "unknown"
            else:
                return await self.async_step_district()

        return await self._async_show_form(USER_SCHEMA, errors)

    async def _async_show_form(
        self, data_schema: vol.Schema, errors: dict[str, str]
    ) -> FlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            description_placeholders={
                "note": "Sign in with your Edulog Parent Portal Lite account."
            },
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # district step                                                      #
    # ------------------------------------------------------------------ #
    async def async_step_district(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client is not None
            tenant_id = str(user_input[CONF_TENANT_ID])
            district = next(
                (d for d in self._districts if str(d.get("tenantId")) == tenant_id),
                {},
            )
            try:
                rows = await asyncio.to_thread(
                    self._client.followed_buses, tenant_id
                )
            except EdulogAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Edulog followed-bus lookup failed")
                errors["base"] = "unknown"
            else:
                if not rows:
                    errors["base"] = "no_buses"
                else:
                    name = district.get("tenantDisplayName") or district.get(
                        "tenantName"
                    ) or tenant_id
                    existing = await self.async_set_unique_id(
                        f"{self._client.email}:{tenant_id}"
                    )
                    if existing:
                        self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_EMAIL: self._client.email,
                            CONF_PASSWORD: self._client.password,
                            CONF_TENANT_ID: tenant_id,
                            CONF_TENANT_NAME: name,
                            CONF_TENANT_CODE: str(
                                district.get("registrationCode") or ""
                            ),
                            CONF_VEHICLE_IDS: [r["vehicleId"] for r in rows],
                        },
                    )

        # One subscription row per registration code, but several codes can
        # map to the SAME tenant (e.g. two schools in one district). An entry
        # tracks per tenant (unique_id = email:tenantId), so list unique
        # tenants, collapsed by their codes.
        tenants: dict[str, dict[str, Any]] = {}
        for d in self._districts:
            tid = str(d.get("tenantId"))
            t = tenants.setdefault(
                tid,
                {
                    "name": d.get("tenantDisplayName")
                    or d.get("tenantName")
                    or tid,
                    "codes": [],
                    "active": False,
                },
            )
            if d.get("registrationCode") is not None:
                t["codes"].append(str(d["registrationCode"])[:4] + "...")
            t["active"] = t["active"] or d.get("status") == "Active"

        options = [
            {
                "value": tid,
                "label": t["name"]
                + (
                    f"  (codes {', '.join(t['codes'])}, "
                    f"{('Active' if t['active'] else 'inactive')})"
                )
                if t["codes"]
                else "",
            }
            for tid, t in sorted(
                tenants.items(), key=lambda kv: 0 if kv[1]["active"] else 1
            )
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_TENANT_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="district",
            data_schema=schema,
            description_placeholders={
                "note": (
                    "This district and every bus you follow in it will be "
                    "tracked. Add another integration entry to track a second "
                    "district."
                )
            },
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # reauth (credentials rejected by the service)                       #
    # ------------------------------------------------------------------ #
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._get_entry_to_reauth()
            try:
                client = EdulogClient(
                    entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
                await asyncio.to_thread(client.login)
            except EdulogAuthError:
                errors[CONF_PASSWORD] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Edulog reauth error")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="success")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # options                                                            #
    # ------------------------------------------------------------------ #
    @callback
    def async_get_options_flow(self, entry: ConfigEntry) -> OptionsFlow:
        return EdulogOptionsFlowHandler(entry)


class EdulogOptionsFlowHandler(OptionsFlow):
    """Poll interval, arrival threshold, optional watch-location override."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = dict(user_input)
            # empty watch location = fall back to HA home location
            for key in (CONF_WATCH_LAT, CONF_WATCH_LNG):
                options[key] = float(user_input[key]) if user_input.get(key) else None
            self.hass.config_entries.async_update_entry(self._entry, options=options)
            # reload so the coordinator picks up the new poll interval
            await self.hass.config_entries.async_reload(self._entry.entry_id)
            return self.async_create_entry(data={})

        options = self._entry.options or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.In(list(POLL_INTERVAL_CHOICES)),
                vol.Required(
                    CONF_THRESHOLD_M,
                    default=options.get(CONF_THRESHOLD_M, DEFAULT_THRESHOLD_M),
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=50000)
                ),
                vol.Optional(
                    CONF_WATCH_LAT,
                    default=options.get(CONF_WATCH_LAT) or "",
                    description={"suggested_value": options.get(CONF_WATCH_LAT) or ""},
                ): _COORD,
                vol.Optional(
                    CONF_WATCH_LNG,
                    default=options.get(CONF_WATCH_LNG) or "",
                    description={"suggested_value": options.get(CONF_WATCH_LNG) or ""},
                ): _COORD,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "note": (
                    "Poll interval: keep ≥ 30 s — this is a third-party "
                    "production service. Watch location: leave empty to use "
                    "the Home Assistant home location; otherwise set the "
                    "place the bus should be measured against (e.g. the "
                    "school or your street)."
                )
            },
            errors=errors,
        )


# NOTE: the entry stores the account password on purpose — it's the only
# way the coordinator can auto-relogin (Keycloak/legacy) when tokens die or
# HA restarts, without re-prompting the user through the config flow.
# Diagnostics never expose it.
