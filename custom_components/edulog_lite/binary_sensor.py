"""Per-bus "near watch location" binary sensor — the arrival trigger.

On   when the bus is inside the configured threshold (default 500 m).
Unknown when there is no fresh GPS fix (bus absent from the live-event
result, or its last fix is older than STALE_AFTER_S) — unknown rather than
off so automations cannot mistake "end of day" for "still far away".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EdulogDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: EdulogDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EdulogBusNearSensor(coordinator, vehicle)
        for vehicle in coordinator.vehicles.values()
    )


class EdulogBusNearSensor(CoordinatorEntity, BinarySensorEntity):
    """Turns on when the bus is within the arrival threshold.

    Subclasses CoordinatorEntity so HA re-renders the state on every
    coordinator refresh — a plain BinarySensorEntity holding `coordinator`
    only would stay frozen after the initial add.
    """

    _attr_has_entity_name = True
    _attr_name = "Arriving"

    def __init__(
        self,
        coordinator: EdulogDataUpdateCoordinator,
        vehicle: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        self.vehicle = vehicle
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}/{vehicle['vehicleId']}/near"

    @property
    def device_info(self) -> DeviceInfo:
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, self.vehicle["vehicleId"])},
            "name": self.vehicle["name"],
            "manufacturer": "KarrosTech",
        }
        if self.coordinator.via_device_id:
            info["via_device_id"] = self.coordinator.via_device_id
        return info

    @property
    def state(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        rec = self.coordinator.data.get(self.vehicle["vehicleId"])
        if rec is None or not rec["valid"]:
            return None  # unknown: no fresh fix
        return rec["distance"] <= self.coordinator.threshold_m()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        rec = self.coordinator.data.get(self.vehicle["vehicleId"])
        if rec is None:
            return None
        return {
            "threshold_m": self.coordinator.threshold_m(),
            "route_code": rec.get("routeCode"),
        }
