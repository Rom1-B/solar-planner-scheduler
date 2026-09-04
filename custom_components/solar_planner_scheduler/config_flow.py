"""Config flow for Solar Planner Scheduler.

Base settings (forecast/production/etc entities) are set once at initial setup; devices and fixed
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
    CONF_AUTO_DAYS,
    CONF_CONSUMPTION_ENTITY,
    CONF_DEVICES,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PRODUCTION_ENTITY,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_SUBSCRIPTION_PRICE_MONTHLY,
    CONF_TARIFF_BANDS,
    DEFAULT_MAX_SIMULTANEOUS_POWER,
    DOMAIN,
    WEEKDAYS,
)


def _base_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    # suggested_value, not default=: a plain default="" fails the selector's own validation
    # (neither a valid entity ID nor UUID) whenever the field is left blank.
    defaults = defaults or {}
    # device_class filters a picker's suggestions only, not a validation constraint.
    energy_sensor = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="energy"))
    power_sensor = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power"))
    return vol.Schema(
        {
            vol.Required(
                CONF_FORECAST_ENTITY, description={"suggested_value": defaults.get(CONF_FORECAST_ENTITY)}
            ): energy_sensor,
            vol.Optional(
                CONF_FORECAST_TOMORROW_ENTITY,
                description={"suggested_value": defaults.get(CONF_FORECAST_TOMORROW_ENTITY)},
            ): energy_sensor,
            vol.Optional(
                CONF_PRODUCTION_ENTITY, description={"suggested_value": defaults.get(CONF_PRODUCTION_ENTITY)}
            ): power_sensor,
            vol.Optional(
                CONF_CONSUMPTION_ENTITY, description={"suggested_value": defaults.get(CONF_CONSUMPTION_ENTITY)}
            ): power_sensor,
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
            vol.Optional(CONF_POWER_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
        }
    )


def _edit_device_schema(device: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_POWER_SENSOR, description={"suggested_value": device.get(CONF_POWER_SENSOR)}
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power")),
        }
    )


_PHASE_LINE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(min|h)\s*@\s*(\d+(?:\.\d+)?)\s*w\s*$", re.IGNORECASE)


class _PhaseParseError(Exception):
    """A line in the phases text field doesn't match `<duration>min@<watts>W` or `<duration>h@<watts>W`."""

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
        amount, unit, power_w = match.groups()
        minutes = float(amount) * 60 if unit.lower() == "h" else float(amount)
        phases.append({CONF_MINUTES: round(minutes), CONF_POWER_W: float(power_w)})
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


def _program_phases_schema(default_text: str = "", default_days: list[str] | None = None) -> vol.Schema:
    # Unchecked by default means on-demand (runs when picked), not "never runs".
    return vol.Schema(
        {
            vol.Required("phases", default=default_text): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional(CONF_AUTO_DAYS, default=default_days or []): selector.SelectSelector(
                selector.SelectSelectorConfig(options=WEEKDAYS, multiple=True, mode=selector.SelectSelectorMode.LIST)
            ),
        }
    )


_TARIFF_LINE_RE = re.compile(r"^\s*([01]\d|2[0-3]):([0-5]\d)\s*@\s*(\d+(?:\.\d+)?)\s*$")


class _TariffParseError(Exception):
    """A line in the tariff bands text field doesn't match `<HH:MM>@<price>`."""

    def __init__(self, error_key: str) -> None:
        self.error_key = error_key
        super().__init__(error_key)


def _parse_tariff_bands(text: str) -> list[dict[str, Any]]:
    bands: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _TARIFF_LINE_RE.match(line)
        if match is None:
            raise _TariffParseError("invalid_tariff_line")
        hour, minute, price = match.groups()
        bands.append({"start": f"{hour}:{minute}", "price": float(price)})
    if not bands:
        raise _TariffParseError("empty_tariff_bands")
    return sorted(bands, key=lambda b: b["start"])


def _tariff_bands_to_text(bands: list[dict[str, Any]]) -> str:
    return "\n".join(f"{b['start']}@{b['price']:g}" for b in bands)


def _tariff_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_PRICE_TRACKING_ENABLED, default=defaults.get(CONF_PRICE_TRACKING_ENABLED, False)
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_SUBSCRIPTION_PRICE_MONTHLY, default=defaults.get(CONF_SUBSCRIPTION_PRICE_MONTHLY, 0.0)
            ): vol.Coerce(float),
            vol.Required(
                "tariff_bands_text", default=defaults.get("tariff_bands_text", "00:00@0.22")
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        }
    )


def _fixed_load_meta_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_START_TIME): selector.TimeSelector(),
        }
    )


class SolarPlannerSchedulerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup — base entities only."""

    VERSION = 4

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
            menu_options=["edit_base", "edit_tariff", "devices_menu", "fixed_loads_menu"],
        )

    async def async_step_devices_menu(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(step_id="devices_menu", menu_options=["add_device", "manage_device", "remove_device"])

    async def async_step_fixed_loads_menu(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="fixed_loads_menu", menu_options=["add_fixed_load", "edit_fixed_load", "remove_fixed_load"]
        )

    async def async_step_edit_base(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # Merged, not replaced: entry.data also holds the tariff fields from async_step_edit_tariff.
            new_data = {**self.config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return await self.async_step_init()
        return self.async_show_form(step_id="edit_base", data_schema=_base_schema(self.config_entry.data))

    async def async_step_edit_tariff(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            try:
                tariff_bands = _parse_tariff_bands(user_input["tariff_bands_text"])
            except _TariffParseError as err:
                return self.async_show_form(
                    step_id="edit_tariff", data_schema=_tariff_schema(user_input), errors={"tariff_bands_text": err.error_key}
                )
            new_data = {
                **self.config_entry.data,
                CONF_PRICE_TRACKING_ENABLED: user_input[CONF_PRICE_TRACKING_ENABLED],
                CONF_SUBSCRIPTION_PRICE_MONTHLY: user_input.get(CONF_SUBSCRIPTION_PRICE_MONTHLY, 0.0),
                CONF_TARIFF_BANDS: tariff_bands,
            }
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return await self.async_step_init()
        current = self.config_entry.data
        defaults = {
            **current,
            "tariff_bands_text": _tariff_bands_to_text(current.get(CONF_TARIFF_BANDS) or [{"start": "00:00", "price": 0.22}]),
        }
        return self.async_show_form(step_id="edit_tariff", data_schema=_tariff_schema(defaults))

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None):
        # Starts with no programs; add one via "Add a program".
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
            return await self._finish_step("devices_menu")
        return self.async_show_form(step_id="add_device", data_schema=_device_schema())

    async def async_step_manage_device(self, user_input: dict[str, Any] | None = None):
        """Pick which device to manage (step 1 of 2: pick -> that device's own menu).

        The name isn't editable from device_detail: it's the key the coordinator's Store and every
        entity's unique_id are built from, so renaming here would orphan already-committed state.
        """
        if not self._devices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._editing_device_name = user_input[CONF_NAME]
            return await self.async_step_device_detail()
        names = [d[CONF_NAME] for d in self._devices]
        return self.async_show_form(step_id="manage_device", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)}))

    async def async_step_device_detail(self, user_input: dict[str, Any] | None = None):
        """This device's own menu: its power sensor, and its programs (no more re-picking the
        device, unlike the old flat top-level "Programs" menu).

        The title can't show the device name: HA's menu step doesn't substitute
        description_placeholders into its title (confirmed live — the raw "{device}" template
        rendered with a formatjs MISSING_VALUE error), unlike a form step's description, which
        does (used below in add_program/edit_program/remove_program/edit_device_power_sensor).
        """
        return self.async_show_menu(
            step_id="device_detail", menu_options=["edit_device_power_sensor", "add_program", "edit_program", "remove_program"]
        )

    async def async_step_edit_device_power_sensor(self, user_input: dict[str, Any] | None = None):
        device_name = self._editing_device_name
        device = next(d for d in self._devices if d[CONF_NAME] == device_name)
        if user_input is not None:
            self._devices = [
                {**d, CONF_POWER_SENSOR: user_input.get(CONF_POWER_SENSOR, "")} if d[CONF_NAME] == device_name else d
                for d in self._devices
            ]
            return await self._finish_step("device_detail")
        return self.async_show_form(
            step_id="edit_device_power_sensor",
            data_schema=_edit_device_schema(device),
            description_placeholders={"device": device_name},
        )

    async def async_step_remove_device(self, user_input: dict[str, Any] | None = None):
        if not self._devices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._devices = [d for d in self._devices if d[CONF_NAME] != user_input[CONF_NAME]]
            return await self._finish_step("devices_menu")
        names = [d[CONF_NAME] for d in self._devices]
        return self.async_show_form(step_id="remove_device", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)}))

    async def async_step_add_program(self, user_input: dict[str, Any] | None = None):
        """Add a new named, multi-phase program to the device being managed (device_detail)."""
        device_name = self._editing_device_name
        device = next(d for d in self._devices if d[CONF_NAME] == device_name)
        schema = vol.Schema({vol.Required("program_name"): str})
        if user_input is not None:
            existing_names = [p[CONF_NAME] for p in device.get(CONF_PROGRAMS, [])]
            if user_input["program_name"] in existing_names:
                return self.async_show_form(step_id="add_program", data_schema=schema, errors={"program_name": "duplicate_program"})
            self._new_program_name = user_input["program_name"]
            return await self.async_step_add_program_phases()
        return self.async_show_form(
            step_id="add_program", data_schema=schema, description_placeholders={"device": device_name}
        )

    async def async_step_add_program_phases(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            try:
                phases = _parse_phases(user_input["phases"])
            except _PhaseParseError as err:
                return self.async_show_form(
                    step_id="add_program_phases",
                    data_schema=_program_phases_schema(user_input["phases"], user_input.get(CONF_AUTO_DAYS)),
                    errors={"phases": err.error_key},
                )
            device_name = self._editing_device_name
            new_program = {
                CONF_NAME: self._new_program_name,
                CONF_POWER_PROFILE: phases,
                CONF_AUTO_DAYS: user_input.get(CONF_AUTO_DAYS, []),
            }
            self._devices = [
                {**d, CONF_PROGRAMS: [*d.get(CONF_PROGRAMS, []), new_program]} if d[CONF_NAME] == device_name else d
                for d in self._devices
            ]
            return await self._finish_step("device_detail")
        return self.async_show_form(step_id="add_program_phases", data_schema=_program_phases_schema())

    async def async_step_edit_program(self, user_input: dict[str, Any] | None = None):
        """Pick which of the managed device's programs to edit (device already known: device_detail)."""
        device_name = self._editing_device_name
        device = next(d for d in self._devices if d[CONF_NAME] == device_name)
        if not device.get(CONF_PROGRAMS):
            return self.async_abort(reason="no_programs")
        if user_input is not None:
            self._editing_program_name = user_input["program_name"]
            return await self.async_step_edit_program_phases()
        program_names = [p[CONF_NAME] for p in device[CONF_PROGRAMS]]
        return self.async_show_form(
            step_id="edit_program",
            data_schema=vol.Schema({vol.Required("program_name"): vol.In(program_names)}),
            description_placeholders={"device": device_name},
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
                    data_schema=_program_phases_schema(user_input["phases"], user_input.get(CONF_AUTO_DAYS)),
                    errors={"phases": err.error_key},
                )
            device_name = self._editing_device_name
            program_name = self._editing_program_name
            auto_days = user_input.get(CONF_AUTO_DAYS, [])
            self._devices = [
                {
                    **d,
                    CONF_PROGRAMS: [
                        {**p, CONF_POWER_PROFILE: phases, CONF_AUTO_DAYS: auto_days} if p[CONF_NAME] == program_name else p
                        for p in d[CONF_PROGRAMS]
                    ],
                }
                if d[CONF_NAME] == device_name
                else d
                for d in self._devices
            ]
            return await self._finish_step("device_detail")
        return self.async_show_form(
            step_id="edit_program_phases",
            data_schema=_program_phases_schema(_phases_to_text(program[CONF_POWER_PROFILE]), program.get(CONF_AUTO_DAYS, [])),
        )

    async def async_step_remove_program(self, user_input: dict[str, Any] | None = None):
        """Pick which of the managed device's programs to remove (device already known: device_detail)."""
        device_name = self._editing_device_name
        device = next(d for d in self._devices if d[CONF_NAME] == device_name)
        if not device.get(CONF_PROGRAMS):
            return self.async_abort(reason="no_removable_programs")
        if user_input is not None:
            removed_name = user_input["program_name"]
            self._devices = [
                {**d, CONF_PROGRAMS: [p for p in d[CONF_PROGRAMS] if p[CONF_NAME] != removed_name]}
                if d[CONF_NAME] == device_name
                else d
                for d in self._devices
            ]
            # Forget the removed program in the coordinator's own store too.
            coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
            if coordinator is not None:
                await coordinator.async_forget_program(device_name, removed_name)
            return await self._finish_step("device_detail")
        program_names = [p[CONF_NAME] for p in device[CONF_PROGRAMS]]
        return self.async_show_form(
            step_id="remove_program",
            data_schema=vol.Schema({vol.Required("program_name"): vol.In(program_names)}),
            description_placeholders={"device": device_name},
        )

    async def async_step_add_fixed_load(self, user_input: dict[str, Any] | None = None):
        """Name and daily start time of the new fixed load (step 1 of 2: meta -> phases)."""
        if user_input is not None:
            self._editing_fixed_load_name = user_input[CONF_NAME]
            self._new_fixed_load_start_time = user_input[CONF_START_TIME]
            return await self.async_step_add_fixed_load_phases()
        return self.async_show_form(step_id="add_fixed_load", data_schema=_fixed_load_meta_schema())

    async def async_step_add_fixed_load_phases(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            try:
                phases = _parse_phases(user_input["phases"])
            except _PhaseParseError as err:
                return self.async_show_form(
                    step_id="add_fixed_load_phases",
                    data_schema=_phases_schema(user_input["phases"]),
                    errors={"phases": err.error_key},
                )
            self._fixed_loads.append(
                {
                    CONF_NAME: self._editing_fixed_load_name,
                    CONF_START_TIME: self._new_fixed_load_start_time,
                    CONF_POWER_PROFILE: phases,
                }
            )
            return await self._finish_step("fixed_loads_menu")
        return self.async_show_form(step_id="add_fixed_load_phases", data_schema=_phases_schema())

    async def async_step_edit_fixed_load(self, user_input: dict[str, Any] | None = None):
        """Pick which fixed load's phases to edit (step 1 of 2: pick -> phases)."""
        if not self._fixed_loads:
            return self.async_abort(reason="no_fixed_loads")
        if user_input is not None:
            self._editing_fixed_load_name = user_input[CONF_NAME]
            return await self.async_step_edit_fixed_load_phases()
        names = [f[CONF_NAME] for f in self._fixed_loads]
        return self.async_show_form(
            step_id="edit_fixed_load", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)})
        )

    async def async_step_edit_fixed_load_phases(self, user_input: dict[str, Any] | None = None):
        load_name = self._editing_fixed_load_name
        load = next(f for f in self._fixed_loads if f[CONF_NAME] == load_name)
        if user_input is not None:
            try:
                phases = _parse_phases(user_input["phases"])
            except _PhaseParseError as err:
                return self.async_show_form(
                    step_id="edit_fixed_load_phases",
                    data_schema=_phases_schema(user_input["phases"]),
                    errors={"phases": err.error_key},
                )
            self._fixed_loads = [
                {**f, CONF_POWER_PROFILE: phases} if f[CONF_NAME] == load_name else f for f in self._fixed_loads
            ]
            return await self._finish_step("fixed_loads_menu")
        return self.async_show_form(
            step_id="edit_fixed_load_phases", data_schema=_phases_schema(_phases_to_text(load[CONF_POWER_PROFILE]))
        )

    async def async_step_remove_fixed_load(self, user_input: dict[str, Any] | None = None):
        if not self._fixed_loads:
            return self.async_abort(reason="no_fixed_loads")
        if user_input is not None:
            self._fixed_loads = [f for f in self._fixed_loads if f[CONF_NAME] != user_input[CONF_NAME]]
            return await self._finish_step("fixed_loads_menu")
        names = [f[CONF_NAME] for f in self._fixed_loads]
        return self.async_show_form(step_id="remove_fixed_load", data_schema=vol.Schema({vol.Required(CONF_NAME): vol.In(names)}))

    async def _finish_step(self, return_to: str = "init"):
        """Persist the pending change and return to the menu instead of closing the flow.

        Returns to the submenu the action started from (e.g. "devices_menu"), not the top-level
        menu, so a run of several actions on the same category doesn't need renavigating each time.
        """
        self.hass.config_entries.async_update_entry(self.config_entry, options=self._current_options())
        return await getattr(self, f"async_step_{return_to}")()

    def _current_options(self) -> dict[str, Any]:
        return {CONF_DEVICES: self._devices, CONF_FIXED_LOADS: self._fixed_loads}
