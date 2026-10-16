"""Data coordinator: one per config entry (district), polls all of that
district's followed buses with the app's own batch endpoint."""

from __future__ import annotations

import logging
import math
import time
from datetime import timedelta
from typing import Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EdulogApiError, EdulogAuthError, EdulogClient
from .const import (
    CONF_POLL_INTERVAL,
    CONF_TENANT_ID,
    CONF_TENANT_NAME,
    CONF_THRESHOLD_M,
    CONF_WATCH_LAT,
    CONF_WATCH_LNG,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_THRESHOLD_M,
    STALE_AFTER_S,
)

_LOGGER = logging.getLogger(__package__)

#: How often the followed-bus list is re-fetched (it can change during the
#: school year).
VEHICLE_RESYNC_S = 86400


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


class EdulogDataUpdateCoordinator(DataUpdateCoordinator):
    """Polls the district's followed buses.

    `data` shape: {vehicleId: {vehicleId, name, routeCode, latitude, longitude,
    last_update (epoch ms | None), distance (m | None), bearing (deg | None),
    valid (bool)}}.
    """

    def __init__(self, hass, entry, client: EdulogClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"edulog_lite {entry.data[CONF_TENANT_NAME]}",
            update_interval=timedelta(
                seconds=int(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
            ),
        )
        self.client = client
        self.vehicles: dict[str, dict[str, Any]] = {}
        self._vehicle_sync_at = 0.0
        #: id of the district "hub" device; set by __init__.py after it is
        #: registered, used by the platforms' via_device_id.
        self.via_device_id: str | None = None

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # Destination for distance/bearing: options override, else the
        # configured Home Assistant home location (hass.config is a plain
        # object — safe to read from the event loop here).
        opts = self.config_entry.options
        lat, lng = opts.get(CONF_WATCH_LAT), opts.get(CONF_WATCH_LNG)
        if lat is not None and lng is not None:
            wlat, wlng = float(lat), float(lng)
        else:
            wlat, wlng = self.hass.config.latitude, self.hass.config.longitude
        return await self.hass.async_add_executor_job(self._fetch, wlat, wlng)

    def _fetch(self, wlat: float, wlng: float) -> dict[str, dict[str, Any]]:
        try:
            if not self.vehicles or time.time() - self._vehicle_sync_at > VEHICLE_RESYNC_S:
                self._sync_vehicles(self.config_entry.data[CONF_TENANT_ID])
            ids = list(self.vehicles)
            events = self.client.live_events(ids) if ids else []
        except EdulogAuthError:
            # stored credentials rejected — surface as an auth problem so HA
            # parks the entry instead of hammering the gateway.
            raise ConfigEntryAuthFailed(self.config_entry.entry_id) from None
        except EdulogApiError as ex:
            raise UpdateFailed(str(ex)) from ex

        now_ms = time.time() * 1000
        stale_after_ms = STALE_AFTER_S * 1000
        evs = {str(e.get("vehicleId")): e for e in events}

        data: dict[str, dict[str, Any]] = {}
        for vid, meta in self.vehicles.items():
            e = evs.get(vid)
            lat = e.get("latitude") if e else None
            lng = e.get("longitude") if e else None
            upd = int(e["lastUpdateTime"]) if e and e.get("lastUpdateTime") else None
            valid = (
                lat is not None
                and lng is not None
                and upd is not None
                and (now_ms - upd) <= stale_after_ms
            )
            data[vid] = {
                **meta,
                "vehicleId": vid,
                "latitude": lat if valid else None,
                "longitude": lng if valid else None,
                # keep showing the (stale) fix time — it explains why
                # distance is unknown ("last fix 2 h ago" is informative).
                "last_update": upd if (valid or upd) else None,
                "distance": round(haversine_m(lat, lng, wlat, wlng), 1) if valid else None,
                "bearing": round(bearing_deg(lat, lng, wlat, wlng), 1) if valid else None,
                "valid": valid,
            }
        return data

    def _sync_vehicles(self, tenant: str) -> None:
        try:
            rows = self.client.followed_buses(tenant)
        except EdulogApiError:
            if self.vehicles:
                _LOGGER.warning(
                    "Edulog: vehicle re-sync failed; keeping %d cached vehicle(s)",
                    len(self.vehicles),
                )
                return
            raise
        self.vehicles = {str(r["vehicleId"]): r for r in rows}
        self._vehicle_sync_at = time.time()
        _LOGGER.info(
            "Edulog: synced %d followed bus(es) for %s",
            len(self.vehicles),
            self.config_entry.data[CONF_TENANT_NAME],
        )

    # ------------------------------------------------------------------ #
    def threshold_m(self) -> float:
        """Arrival threshold, read live so options changes apply per poll."""
        return float(
            self.config_entry.options.get(CONF_THRESHOLD_M, DEFAULT_THRESHOLD_M)
        )
