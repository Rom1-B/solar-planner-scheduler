"""System Health platform — a quick coordinator status snapshot, no server/log access needed.

Shown on Settings > System > Repairs > System Health. Lighter than diagnostics.py's full Store
dump: just enough to tell at a glance whether anything needs attention.
"""

from __future__ import annotations

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICES, CONF_NAME, CONF_PROGRAMS, DOMAIN


@callback
def async_register(hass: HomeAssistant, register: system_health.SystemHealthRegistration) -> None:
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict:
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        return {"status": "not set up"}

    active_programs = 0
    for coordinator in coordinators:
        for device in coordinator.entry.options.get(CONF_DEVICES, []):
            for program in device.get(CONF_PROGRAMS, []):
                if coordinator.is_program_active(device[CONF_NAME], program[CONF_NAME], program):
                    active_programs += 1

    # Counts real open Repairs (see coordinator.py's _note_failed_to_start), not a
    # recomputed/duplicated threshold check — this is the same registry the Repairs page reads.
    open_repairs = sum(1 for (domain, _issue_id) in ir.async_get(hass).issues if domain == DOMAIN)

    info = {
        "last_update_success": all(coordinator.last_update_success for coordinator in coordinators),
        "active_programs": active_programs,
        "open_repairs": open_repairs,
    }
    pv_forecast_coordinators = [c.pv_forecast_coordinator for c in coordinators if c.pv_forecast_coordinator is not None]
    if pv_forecast_coordinators:
        info["pv_forecast_last_update_success"] = all(c.last_update_success for c in pv_forecast_coordinators)
    return info
