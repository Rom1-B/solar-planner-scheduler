"""Config flow for Solar Planner Scheduler.

Base settings (forecast/surplus/etc entities) are set once at initial setup; devices and fixed
loads are managed afterwards through the options flow so they can be added/removed without
recreating the whole config entry.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CONSUMPTION_ENTITY,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PRODUCTION_ENTITY,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    CONF_START_TIME,
    CONF_SURPLUS_ENTITY,
    DEFAULT_MAX_SIMULTANEOUS_POWER,
    DOMAIN,
)


def _base_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_FORECAST_ENTITY, default=defaults.get(CONF_FORECAST_ENTITY, "")): selector.EntitySelector(),
            vol.Optional(
                CONF_FORECAST_TOMORROW_ENTITY, default=defaults.get(CONF_FORECAST_TOMORROW_ENTITY, "")
            ): selector.EntitySelector(),
            vol.Required(CONF_SURPLUS_ENTITY, default=defaults.get(CONF_SURPLUS_ENTITY, "")): selector.EntitySelector(),
            vol.Optional(CONF_PRODUCTION_ENTITY, default=defaults.get(CONF_PRODUCTION_ENTITY, "")): selector.EntitySelector(),
            vol.Optional(CONF_CONSUMPTION_ENTITY, default=defaults.get(CONF_CONSUMPTION_ENTITY, "")): selector.EntitySelector(),
            vol.Required(
                CONF_MAX_SIMULTANEOUS_POWER,
                default=defaults.get(CONF_MAX_SIMULTANEOUS_POWER, DEFAULT_MAX_SIMULTANEOUS_POWER),
            ): vol.Coerce(int),
        }
    )


def _device_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Optional(CONF_POWER_SENSOR, default=""): selector.EntitySelector(),
            vol.Required(CONF_POWER_W): vol.Coerce(float),
            vol.Required(CONF_DURATION_MIN): vol.Coerce(int),
        }
    )


def _phase_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MINUTES): vol.Coerce(int),
            vol.Required(CONF_POWER_W): vol.Coerce(float),
            vol.Required("add_another_phase", default=False): bool,
        }
    )


def _fixed_load_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_START_TIME): str,
            vol.Required(CONF_POWER_W): vol.Coerce(float),
            vol.Required(CONF_DURATION_MIN): vol.Coerce(int),
        }
    )


class SolarPlannerSchedulerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup — base entities only."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="Solar Planner Scheduler",
                data=user_input,
                options={CONF_DEVICES: [], CONF_FIXED_LOADS: []},
            )
        return self.async_show_form(step_id="user", data_schema=_base_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "SolarPlannerSchedulerOptionsFlow":
        return SolarPlannerSchedulerOptionsFlow(config_entry)


class SolarPlannerSchedulerOptionsFlow(config_entries.OptionsFlow):
    """Add/remove devices and fixed loads, or edit the base entities.

    Each action finishes the flow (the user reopens "Configure" to do another) — a menu that loops
    back to itself is a natural follow-up but not needed for a first working version.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._devices: list[dict[str, Any]] = list(config_entry.options.get(CONF_DEVICES, []))
        self._fixed_loads: list[dict[str, Any]] = list(config_entry.options.get(CONF_FIXED_LOADS, []))

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "edit_base",
                "add_device",
                "remove_device",
                "add_program",
                "remove_program",
                "add_fixed_load",
                "remove_fixed_load",
            ],
        )

    async def async_step_edit_base(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self.hass.config_entries.async_update_entry(self.config_entry, data=user_input)
            return self.async_create_entry(title="", data=self._current_options())
        return self.async_show_form(step_id="edit_base", data_schema=_base_schema(self.config_entry.data))

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            name = user_input[CONF_NAME]
            program = {
                CONF_NAME: name,
                CONF_POWER_W: user_input[CONF_POWER_W],
                CONF_DURATION_MIN: user_input[CONF_DURATION_MIN],
            }
            self._devices.append(
                {
                    CONF_NAME: name,
                    CONF_POWER_SENSOR: user_input.get(CONF_POWER_SENSOR, ""),
                    CONF_PROGRAMS: [program],
                    CONF_SELECTED_PROGRAM: name,
                }
            )
            return self.async_create_entry(title="", data=self._current_options())
        return self.async_show_form(step_id="add_device", data_schema=_device_schema())

    async def async_step_remove_device(self, user_input: dict[str, Any] | None = None):
        if not self._devices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._devices = [d for d in self._devices if d[CONF_NAME] != user_input[CONF_NAME]]
            return self.async_create_entry(title="", data=self._current_options())
        names = [d[CONF_NAME] for d in self._devices]
        return self.async_show_form(step_id="remove_device", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)}))

    async def async_step_add_program(self, user_input: dict[str, Any] | None = None):
        """Add a new named, multi-phase program to an existing device (on top of any it already has)."""
        if not self._devices:
            return self.async_abort(reason="no_devices")
        device_names = [d[CONF_NAME] for d in self._devices]
        schema = vol.Schema({vol.Required(CONF_NAME): vol.In(device_names), vol.Required("program_name"): str})
        if user_input is not None:
            device = next(d for d in self._devices if d[CONF_NAME] == user_input[CONF_NAME])
            existing_names = [p[CONF_NAME] for p in device.get(CONF_PROGRAMS, [])]
            if user_input["program_name"] in existing_names:
                return self.async_show_form(step_id="add_program", data_schema=schema, errors={"program_name": "duplicate_program"})
            self._editing_device_name = user_input[CONF_NAME]
            self._new_program_name = user_input["program_name"]
            self._phases: list[dict[str, Any]] = []
            return await self.async_step_add_phase()
        return self.async_show_form(step_id="add_program", data_schema=schema)

    async def async_step_add_phase(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._phases.append({CONF_MINUTES: user_input[CONF_MINUTES], CONF_POWER_W: user_input[CONF_POWER_W]})
            if user_input["add_another_phase"]:
                return self.async_show_form(step_id="add_phase", data_schema=_phase_schema())
            device_name = self._editing_device_name
            new_program = {CONF_NAME: self._new_program_name, CONF_POWER_PROFILE: self._phases}
            self._devices = [
                {**d, CONF_PROGRAMS: [*d.get(CONF_PROGRAMS, []), new_program]} if d[CONF_NAME] == device_name else d
                for d in self._devices
            ]
            return self.async_create_entry(title="", data=self._current_options())
        return self.async_show_form(step_id="add_phase", data_schema=_phase_schema())

    async def async_step_remove_program(self, user_input: dict[str, Any] | None = None):
        devices_with_programs = [d for d in self._devices if len(d.get(CONF_PROGRAMS, [])) > 1]
        if not devices_with_programs:
            return self.async_abort(reason="no_removable_programs")
        device_names = [d[CONF_NAME] for d in devices_with_programs]
        if user_input is not None and CONF_NAME in user_input and "program_name" not in user_input:
            self._removing_device_name = user_input[CONF_NAME]
            device = next(d for d in self._devices if d[CONF_NAME] == self._removing_device_name)
            program_names = [p[CONF_NAME] for p in device[CONF_PROGRAMS]]
            return self.async_show_form(
                step_id="remove_program", data_schema=vol.Schema({vol.Required("program_name"): vol.In(program_names)})
            )
        if user_input is not None and "program_name" in user_input:
            device_name = self._removing_device_name
            self._devices = [
                {**d, CONF_PROGRAMS: [p for p in d[CONF_PROGRAMS] if p[CONF_NAME] != user_input["program_name"]]}
                if d[CONF_NAME] == device_name
                else d
                for d in self._devices
            ]
            return self.async_create_entry(title="", data=self._current_options())
        return self.async_show_form(step_id="remove_program", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(device_names)}))

    async def async_step_add_fixed_load(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._fixed_loads.append(user_input)
            return self.async_create_entry(title="", data=self._current_options())
        return self.async_show_form(step_id="add_fixed_load", data_schema=_fixed_load_schema())

    async def async_step_remove_fixed_load(self, user_input: dict[str, Any] | None = None):
        if not self._fixed_loads:
            return self.async_abort(reason="no_fixed_loads")
        if user_input is not None:
            self._fixed_loads = [f for f in self._fixed_loads if f[CONF_NAME] != user_input[CONF_NAME]]
            return self.async_create_entry(title="", data=self._current_options())
        names = [f[CONF_NAME] for f in self._fixed_loads]
        return self.async_show_form(step_id="remove_fixed_load", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)}))

    def _current_options(self) -> dict[str, Any]:
        return {CONF_DEVICES: self._devices, CONF_FIXED_LOADS: self._fixed_loads}
