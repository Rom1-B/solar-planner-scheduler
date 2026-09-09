"""pytest-homeassistant-custom-component fixture setup.

The harness's own `hass_config_dir` fixture defaults to its bundled `testing_config` directory,
which doesn't contain `custom_components/`. Override it to point at this repo's root instead,
where `custom_components/solar_planner_scheduler` actually lives.
"""

import pathlib

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture
def hass_config_dir() -> str:
    return str(pathlib.Path(__file__).parent.parent)


def register_provider_entities(hass, domain: str, entities: dict[str, dict], entry: MockConfigEntry | None = None):
    """Create (or reuse) a config entry for `domain`, register each entity directly under it
    (config_entry kwarg, no device indirection needed) and set its live state to the given
    attributes. Returns the config_entry_id, for the config_entry-based forecast provider fields
    (Solcast/Helios/forecast.solar all resolve their real entities from this id, not from a
    hand-picked entity_id — see resolve_forecast_sources()/_discover_provider_entities() in
    coordinator.py). Passing no attributes for an entity_id registers it without ever calling
    hass.states.async_set(), i.e. a disabled/no-state entity, to test that discovery skips it.
    """
    if entry is None:
        entry = MockConfigEntry(domain=domain)
        entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for entity_id, attributes in entities.items():
        object_domain, object_id = entity_id.split(".", 1)
        registry.async_get_or_create(
            object_domain, domain, f"{object_id}_uid", suggested_object_id=object_id, config_entry=entry
        )
        if attributes is not None:
            hass.states.async_set(entity_id, "0", attributes)
    return entry.entry_id
