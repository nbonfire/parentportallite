"""GPS device trackers — the bus on map cards / zones.

Each followed bus becomes a `device_tracker` with its live coordinates.
HA's map cards plot any tracker entity that has a position, and the
base class handles the home/zone presence logic for free: create a zone
(e.g. around the school or your street) and the tracker's state becomes
the zone name while the bus is inside it — another arrival trigger.

Like the sensors: stale or missing fixes report no position (None),
which pulls the bus off the map and makes the tracker unknown.
"""

from __future__ import annotations

from typing import Any

# 2026.x layout: TrackerEntity is the GPS base (was GPSTrackerEntity in
# older core); zone membership / home state / map plotting all come from
# the base class given just latitude/longitude.
from homeassistant.components.device_tracker import TrackerEntity
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
        EdulogBusTracker(coordinator, vehicle)
        for vehicle in coordinator.vehicles.values()
    )


class EdulogBusTracker(CoordinatorEntity, TrackerEntity):
    """Live GPS position of one followed bus.

    Subclasses CoordinatorEntity so HA re-renders the position on every
    coordinator refresh — a plain TrackerEntity holding `coordinator`
    only would stay frozen after the initial add.
    """

    _attr_has_entity_name = True
    _attr_name = "GPS"

    def __init__(
        self,
        coordinator: EdulogDataUpdateCoordinator,
        vehicle: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        self.vehicle = vehicle
        self._attr_unique_id = (
            f"{coordinator.config_entry.unique_id}/{vehicle['vehicleId']}/gps"
        )

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

    def _rec(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.vehicle["vehicleId"])

    @property
    def latitude(self) -> float | None:
        rec = self._rec()
        return rec["latitude"] if rec else None

    @property
    def longitude(self) -> float | None:
        rec = self._rec()
        return rec["longitude"] if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self._rec()
        if rec is None:
            return None
        return {
            "route_code": rec.get("routeCode"),
            "distance_m": rec["distance"],
            "bearing": rec["bearing"],
            "stale_fix": bool(rec["last_update"] is not None and rec["distance"] is None),
        }
