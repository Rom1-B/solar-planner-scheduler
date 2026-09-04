"""Self-computed solar production forecast: pvlib fed by an Open-Meteo irradiance forecast.

An alternative to an external forecast entity (e.g. Solcast) for the site's own panels, using
only the parameters an installer would already know (capacity, azimuth, tilt, a flat loss %).
`compute_pv_forecast()` is pure (no `hass`), same philosophy as `scheduling.py`; the HTTP fetch and
the coordinator wrapping it are the only pieces that need `hass`.

`pandas`/`pvlib` are deliberately imported inside compute_pv_forecast(), not at module level: HA's
own blocking-call detector flagged the module-level import live (reading pvlib's METADATA file,
importing scipy.spatial, both synchronous I/O) when this file gets imported from
`async_setup_entry()`. PvForecastCoordinator._async_update_data() always calls this function via
`hass.async_add_executor_job()`, so the deferred import (and the rest of the computation) runs on
the executor thread, never directly on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PV_AZIMUTH,
    CONF_PV_CAPACITY_KWC,
    CONF_PV_LOSS_PCT,
    CONF_PV_TILT,
    CONF_PV_WEATHER_MODEL,
    DOMAIN,
    PV_FORECAST_UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_HOURLY_VARS = "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m,wind_speed_10m"
_FETCH_TIMEOUT_S = 10


async def async_fetch_open_meteo(hass: HomeAssistant, latitude: float, longitude: float, model: str) -> dict:
    """Raw Open-Meteo hourly forecast JSON. Raises on any network/HTTP failure, never swallows one."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": _HOURLY_VARS,
        "forecast_days": 2,
        "timezone": "UTC",
    }
    if model != "best_match":
        params["models"] = model
    session = async_get_clientsession(hass)
    async with asyncio.timeout(_FETCH_TIMEOUT_S):
        response = await session.get(_OPEN_METEO_URL, params=params)
        response.raise_for_status()
        return await response.json()


def compute_pv_forecast(
    weather: dict,
    latitude: float,
    longitude: float,
    elevation: float,
    capacity_kwc: float,
    azimuth: float,
    tilt: float,
    loss_pct: float,
) -> list[dict]:
    """Pure PVWatts-equivalent forecast from an Open-Meteo hourly payload (see async_fetch_open_meteo).

    Returns [{"time": datetime, "w": float}, ...], sorted, same shape _read_forecast_points()
    already produces from a Solcast-style entity: coordinator.py doesn't need to know which one
    it's reading.
    """
    import pandas as pd
    import pvlib

    hourly = weather["hourly"]
    index = pd.to_datetime(hourly["time"], utc=True)
    weather_df = pd.DataFrame(
        {
            "ghi": hourly["shortwave_radiation"],
            "dni": hourly["direct_normal_irradiance"],
            "dhi": hourly["diffuse_radiation"],
            "temp_air": hourly["temperature_2m"],
            "wind_speed": hourly["wind_speed_10m"],
        },
        index=index,
    ).fillna(0.0)

    location = pvlib.location.Location(latitude, longitude, tz="UTC", altitude=elevation)
    pdc0 = capacity_kwc * 1000
    # with_pvwatts() can't infer a cell-temperature model without this: an open-rack glass/glass
    # residential rooftop is the standard, reasonable default (pvlib raises ValueError otherwise).
    temperature_params = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_glass"]
    system = pvlib.pvsystem.PVSystem(
        surface_tilt=tilt,
        surface_azimuth=azimuth,
        module_parameters={"pdc0": pdc0, "gamma_pdc": -0.0034},
        inverter_parameters={"pdc0": pdc0},
        temperature_model_parameters=temperature_params,
    )
    model_chain = pvlib.modelchain.ModelChain.with_pvwatts(system, location)
    model_chain.run_model(weather_df)

    # A single flat loss % applied after the model, not pvlib's own pvwatts_losses() sub-component
    # breakdown (soiling/shading/snow/...): the user only ever configures one blended percentage.
    derate = 1 - loss_pct / 100
    return sorted(
        (
            {"time": ts.to_pydatetime(), "w": max(0.0, float(power) * derate)}
            for ts, power in model_chain.results.ac.items()
        ),
        key=lambda p: p["time"],
    )


class PvForecastCoordinator(DataUpdateCoordinator[list[dict]]):
    """Fetches Open-Meteo and runs pvlib on its own slower cadence, decoupled from the main
    15-min scheduling coordinator: irradiance forecasts don't change that fast.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_pv_forecast",
            update_interval=timedelta(minutes=PV_FORECAST_UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry

    async def _async_update_data(self) -> list[dict]:
        data = self.entry.data
        try:
            weather = await async_fetch_open_meteo(
                self.hass,
                self.hass.config.latitude,
                self.hass.config.longitude,
                data.get(CONF_PV_WEATHER_MODEL, "best_match"),
            )
            # compute_pv_forecast() does its own (deferred) pandas/pvlib imports plus the actual
            # numeric computation, both blocking; never call it directly on the event loop.
            return await self.hass.async_add_executor_job(
                compute_pv_forecast,
                weather,
                self.hass.config.latitude,
                self.hass.config.longitude,
                self.hass.config.elevation,
                data[CONF_PV_CAPACITY_KWC],
                data[CONF_PV_AZIMUTH],
                data[CONF_PV_TILT],
                data.get(CONF_PV_LOSS_PCT, 14.0),
            )
        except Exception as err:  # noqa: BLE001 - any network/parsing/pvlib failure must keep the last good forecast
            raise UpdateFailed(f"Failed to compute the PV forecast: {err}") from err
