"""Diagnostics support — downloadable dump of the coordinator's persisted Store and last update.

No redaction: this integration's config entry holds no secrets (entity IDs, device/program names,
power profiles, thresholds), so entry.data/entry.options are exposed as-is.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry_data": dict(entry.data),
        "entry_options": dict(entry.options),
        **coordinator.diagnostics_snapshot(),
    }
