"""Diagnostics for the Edulog Parent Portal Lite integration.

Secrets handled here: the account password and both JWTs are deliberately
never included; only non-sensitive token metadata is reported.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, DOMAIN

REDACT = "REDACTED"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    client = coordinator.client

    data: dict = {
        "entry_data": {
            **{k: v for k, v in entry.data.items() if k != CONF_PASSWORD},
            CONF_PASSWORD: REDACT,
        },
        "options": dict(entry.options),
        "client": {
            "mode": client.mode,
            "email": client.email,
            "person_id": client.person_id,
            "expires_in": client.expires_in,
            "obtained_at": client.obtained_at,
        },
        "vehicles": {
            "vehicle_ids": list(coordinator.vehicles),
            "synced_at": coordinator._vehicle_sync_at,  # noqa: SLF001
            "last_update_success": coordinator.last_update_success,
            "last_exception": str(coordinator.last_exception)
            if coordinator.last_exception
            else None,
        },
        "live_positions": dict(coordinator.data or {}),
    }
    return data
