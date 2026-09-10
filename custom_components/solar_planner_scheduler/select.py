"""Select platform - which forecast provider (Solcast, Helios Forecast, ...) drives scheduling.

Global to the entry, not per-device: the active source lives in the coordinator's own store, not
entry.data, so switching it never reloads the whole integration (same reasoning as
switch.<device>_<program>_active).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    FORECAST_PROVIDER_HELIOS,
    FORECAST_PROVIDER_LABELS,
    FORECAST_PROVIDER_SOLCAST,
    FORECAST_PROVIDER_WEIGHTED,
)
from .coordinator import SolarPlannerSchedulerCoordinator
from .forecast_providers import FORECAST_COMBINERS, resolve_forecast_sources

_PROVIDER_LABELS = {**FORECAST_PROVIDER_LABELS, "average": "Average", "min": "Min", "weighted": "Weighted"}


def _weighted_available(resolved) -> bool:
    return FORECAST_PROVIDER_SOLCAST in resolved and FORECAST_PROVIDER_HELIOS in resolved


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ForecastSourceSelect(coordinator, entry)])


class ForecastSourceSelect(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SelectEntity):
    """Which configured forecast provider is currently active for scheduling."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_forecast_source"
        self._attr_name = "Solar Planner Scheduler forecast source"

    @property
    def options(self) -> list[str]:
        resolved = resolve_forecast_sources(self._entry.data)
        labels = [_PROVIDER_LABELS.get(p, p) for p in resolved]
        if len(resolved) >= 2:
            labels += [_PROVIDER_LABELS[c] for c in FORECAST_COMBINERS]
        if _weighted_available(resolved):
            labels.append(_PROVIDER_LABELS[FORECAST_PROVIDER_WEIGHTED])
        return labels

    def _resolved_active_provider(self) -> str | None:
        resolved = resolve_forecast_sources(self._entry.data)
        valid = (
            set(resolved)
            | (set(FORECAST_COMBINERS) if len(resolved) >= 2 else set())
            | ({FORECAST_PROVIDER_WEIGHTED} if _weighted_available(resolved) else set())
        )
        active = self.coordinator.active_forecast_source()
        if active not in valid:
            active = next(iter(resolved), None)
        return active

    @property
    def current_option(self) -> str | None:
        active = self._resolved_active_provider()
        return _PROVIDER_LABELS.get(active, active) if active else None

    @property
    def extra_state_attributes(self) -> dict:
        # The raw machine key, not the display label: the card reads this instead of retranslating
        # current_option's label back into a provider key.
        return {"provider": self._resolved_active_provider()}

    async def async_select_option(self, option: str) -> None:
        provider = next((p for p, label in _PROVIDER_LABELS.items() if label == option), option)
        await self.coordinator.async_set_forecast_source(provider)
