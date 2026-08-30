"""Config flow for Solar Planner Scheduler.

Base settings (forecast/surplus/etc entities) are set once at initial setup; devices and fixed
loads are managed afterwards through the options flow so they can be added/removed without
recreating the whole config entry.
"""

from __future__ import annotations

import re
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
    NONE_PROGRAM,
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
        }
    )


_PHASE_LINE_RE = re.compile(r"^\s*(\d+)\s*min\s*@\s*(\d+(?:\.\d+)?)\s*w\s*$", re.IGNORECASE)


class _PhaseParseError(Exception):
    """A line in the phases text field doesn't match `<minutes>min@<watts>W`."""

    def __init__(self, error_key: str) -> None:
        self.error_key = error_key
        super().__init__(error_key)


def _parse_phases(text: str) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _PHASE_LINE_RE.match(line)
        if match is None:
            raise _PhaseParseError("invalid_phase_line")
        minutes, power_w = match.groups()
        phases.append({CONF_MINUTES: int(minutes), CONF_POWER_W: float(power_w)})
    if not phases:
        raise _PhaseParseError("empty_phases")
    return phases


def _phases_to_text(phases: list[dict[str, Any]]) -> str:
    return "\n".join(f"{p[CONF_MINUTES]}min@{p[CONF_POWER_W]:g}W" for p in phases)


def _phases_schema(default_text: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("phases", default=default_text): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        }
    )


def _fixed_load_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_START_TIME): selector.TimeSelector(),
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

    Each action persists its change immediately and returns to the menu (`_finish_step`), so
    several actions can be done in the same "Configure" session instead of reopening it each time.
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
                "edit_program",
                "remove_program",
                "add_fixed_load",
                "remove_fixed_load",
            ],
        )

    async def async_step_edit_base(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self.hass.config_entries.async_update_entry(self.config_entry, data=user_input)
            return await self.async_step_init()
        return self.async_show_form(step_id="edit_base", data_schema=_base_schema(self.config_entry.data))

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None):
        # No auto-created flat program here: a device with a name identical to its only program
        # was a confusing extra option once real named programs exist (see "Add a program"). A
        # freshly added device starts with no programs at all (select entity offers just "None")
        # until one is added.
        if user_input is not None:
            if user_input[CONF_NAME] in [d[CONF_NAME] for d in self._devices]:
                return self.async_show_form(
                    step_id="add_device", data_schema=_device_schema(), errors={CONF_NAME: "duplicate_device"}
                )
            self._devices.append(
                {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_POWER_SENSOR: user_input.get(CONF_POWER_SENSOR, ""),
                    CONF_PROGRAMS: [],
                }
            )
            return await self._finish_step()
        return self.async_show_form(step_id="add_device", data_schema=_device_schema())

    async def async_step_remove_device(self, user_input: dict[str, Any] | None = None):
        if not self._devices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._devices = [d for d in self._devices if d[CONF_NAME] != user_input[CONF_NAME]]
            return await self._finish_step()
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
            return await self.async_step_add_program_phases()
        return self.async_show_form(step_id="add_program", data_schema=schema)

    async def async_step_add_program_phases(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            try:
                phases = _parse_phases(user_input["phases"])
            except _PhaseParseError as err:
                return self.async_show_form(
                    step_id="add_program_phases",
                    data_schema=_phases_schema(user_input["phases"]),
                    errors={"phases": err.error_key},
                )
            device_name = self._editing_device_name
            new_program = {CONF_NAME: self._new_program_name, CONF_POWER_PROFILE: phases}
            self._devices = [
                {**d, CONF_PROGRAMS: [*d.get(CONF_PROGRAMS, []), new_program]} if d[CONF_NAME] == device_name else d
                for d in self._devices
            ]
            return await self._finish_step()
        return self.async_show_form(step_id="add_program_phases", data_schema=_phases_schema())

    async def async_step_edit_program(self, user_input: dict[str, Any] | None = None):
        """Pick which device's program to edit (step 1 of 3: device -> program -> phases)."""
        devices_with_programs = [d for d in self._devices if d.get(CONF_PROGRAMS)]
        if not devices_with_programs:
            return self.async_abort(reason="no_programs")
        if user_input is not None:
            self._editing_device_name = user_input[CONF_NAME]
            return await self.async_step_edit_program_pick()
        device_names = [d[CONF_NAME] for d in devices_with_programs]
        return self.async_show_form(
            step_id="edit_program", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(device_names)})
        )

    async def async_step_edit_program_pick(self, user_input: dict[str, Any] | None = None):
        device = next(d for d in self._devices if d[CONF_NAME] == self._editing_device_name)
        if user_input is not None:
            self._editing_program_name = user_input["program_name"]
            return await self.async_step_edit_program_phases()
        program_names = [p[CONF_NAME] for p in device[CONF_PROGRAMS]]
        return self.async_show_form(
            step_id="edit_program_pick", data_schema=vol.Schema({vol.Required("program_name"): vol.In(program_names)})
        )

    async def async_step_edit_program_phases(self, user_input: dict[str, Any] | None = None):
        device = next(d for d in self._devices if d[CONF_NAME] == self._editing_device_name)
        program = next(p for p in device[CONF_PROGRAMS] if p[CONF_NAME] == self._editing_program_name)
        if user_input is not None:
            try:
                phases = _parse_phases(user_input["phases"])
            except _PhaseParseError as err:
                return self.async_show_form(
                    step_id="edit_program_phases",
                    data_schema=_phases_schema(user_input["phases"]),
                    errors={"phases": err.error_key},
                )
            device_name = self._editing_device_name
            program_name = self._editing_program_name
            self._devices = [
                {
                    **d,
                    CONF_PROGRAMS: [
                        {**p, CONF_POWER_PROFILE: phases} if p[CONF_NAME] == program_name else p
                        for p in d[CONF_PROGRAMS]
                    ],
                }
                if d[CONF_NAME] == device_name
                else d
                for d in self._devices
            ]
            return await self._finish_step()
        return self.async_show_form(
            step_id="edit_program_phases", data_schema=_phases_schema(_phases_to_text(program[CONF_POWER_PROFILE]))
        )

    async def async_step_remove_program(self, user_input: dict[str, Any] | None = None):
        devices_with_programs = [d for d in self._devices if d.get(CONF_PROGRAMS)]
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
            removed_name = user_input["program_name"]

            def _update(d: dict[str, Any]) -> dict[str, Any]:
                if d[CONF_NAME] != device_name:
                    return d
                updated = {**d, CONF_PROGRAMS: [p for p in d[CONF_PROGRAMS] if p[CONF_NAME] != removed_name]}
                if d.get(CONF_SELECTED_PROGRAM) == removed_name:
                    updated[CONF_SELECTED_PROGRAM] = NONE_PROGRAM
                return updated

            self._devices = [_update(d) for d in self._devices]
            return await self._finish_step()
        return self.async_show_form(step_id="remove_program", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(device_names)}))

    async def async_step_add_fixed_load(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._fixed_loads.append(user_input)
            return await self._finish_step()
        return self.async_show_form(step_id="add_fixed_load", data_schema=_fixed_load_schema())

    async def async_step_remove_fixed_load(self, user_input: dict[str, Any] | None = None):
        if not self._fixed_loads:
            return self.async_abort(reason="no_fixed_loads")
        if user_input is not None:
            self._fixed_loads = [f for f in self._fixed_loads if f[CONF_NAME] != user_input[CONF_NAME]]
            return await self._finish_step()
        names = [f[CONF_NAME] for f in self._fixed_loads]
        return self.async_show_form(step_id="remove_fixed_load", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)}))

    async def _finish_step(self):
        """Persist the pending change and return to the menu instead of closing the flow."""
        self.hass.config_entries.async_update_entry(self.config_entry, options=self._current_options())
        return await self.async_step_init()

    def _current_options(self) -> dict[str, Any]:
        return {CONF_DEVICES: self._devices, CONF_FIXED_LOADS: self._fixed_loads}
