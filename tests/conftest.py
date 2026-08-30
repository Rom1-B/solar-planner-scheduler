"""pytest-homeassistant-custom-component fixture setup.

The harness's own `hass_config_dir` fixture defaults to its bundled `testing_config` directory,
which doesn't contain `custom_components/`. Override it to point at this repo's root instead,
where `custom_components/solar_planner_scheduler` actually lives.
"""

import pathlib

import pytest


@pytest.fixture
def hass_config_dir() -> str:
    return str(pathlib.Path(__file__).parent.parent)
