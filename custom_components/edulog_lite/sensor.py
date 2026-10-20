"""Per-bus sensors: distance to watch location, last GPS fix, bearing."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
import datetime as _dd

from .const import DOMAIN
from .coordinator import EdulogDataUpdateCoordinator

KIND_LABELS = {
    "distance": "Distance",
    "gps_last_update": "GPS last update",
    "bearing": "Bearing to watch location",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: EdulogDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EdulogBusSensor(coordinator, vehicle, kind)
        for vehicle in coordinator.vehicles.values()
        for kind in KIND_LABELS
    )


class EdulogBusSensor(CoordinatorEntity, SensorEntity):
    """One attribute (distance / GPS fix time / bearing) of one followed bus.

    Subclasses CoordinatorEntity so HA re-renders the state on every
    coordinator refresh — a plain SensorEntity holding `coordinator`
    only would stay frozen after the initial add.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EdulogDataUpdateCoordinator,
        vehicle: dict[str, Any],
        kind: str,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        self.vehicle = vehicle
        self.kind = kind
        self._attr_name = KIND_LABELS[kind]
        self._attr_unique_id = (
            f"{coordinator.config_entry.unique_id}/{vehicle['vehicleId']}/{kind}"
        )
        if kind == "distance":
            self._attr_device_class = SensorDeviceClass.DISTANCE
            self._attr_native_unit_of_measurement = UnitOfLength.METERS
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif kind == "gps_last_update":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        else:  # bearing
            self._attr_native_unit_of_measurement = "°"

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
    def state(self) -> Any:
        rec = self._rec()
        if rec is None:
            return None
        if self.kind == "distance":
            return rec["distance"]
        if self.kind == "gps_last_update":
            if rec["last_update"] is None:
                return None
            return dt_util.as_local(
                _dd.datetime.fromtimestamp(rec["last_update"] / 1000, tz=_dd.timezone.utc)
            )
        return rec["bearing"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.kind != "distance":
            return None
        rec = self._rec()
        if rec is None:
            return None
        attrs = {"route_code": rec.get("routeCode")}
        # why distance may be unknown: "stale" = a fix exists but is old
        if rec["last_update"] is not None and rec["distance"] is None:
            attrs["stale_fix"] = True
        return attrs
