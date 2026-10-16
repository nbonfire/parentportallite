"""The Edulog Parent Portal Lite integration.

Tracks the live GPS of the buses a parent account follows in a chosen
school district (tenant). One config entry = one district; each entry
exposes per-bus sensors (distance / GPS fix time / bearing) plus a
"near watch location" binary sensor for arrival automations.

This is for the *Lite* companion app only — see FLOW.md for the full
reverse-engineered protocol notes.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .api import EdulogClient
from .const import CONF_PASSWORD, CONF_TENANT_ID, CONF_TENANT_NAME, DOMAIN
from .coordinator import EdulogDataUpdateCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = EdulogClient(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    coordinator = EdulogDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # District "device": parents pick this in the config flow, buses attach
    # to it via via_device so the UI groups everything under the district.
    device_registry = dr.async_get(hass)
    district_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_TENANT_ID])},
        manufacturer="KarrosTech",
        model="Parent Portal Lite",
        name=entry.data[CONF_TENANT_NAME],
    )
    coordinator.via_device_id = district_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        return True
    return False
