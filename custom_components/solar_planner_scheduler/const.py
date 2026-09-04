"""Constants for the Solar Planner Scheduler integration."""

DOMAIN = "solar_planner_scheduler"

CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FORECAST_TOMORROW_ENTITY = "forecast_tomorrow_entity"
CONF_PRODUCTION_ENTITY = "production_entity"
CONF_CONSUMPTION_ENTITY = "consumption_entity"
CONF_MAX_SIMULTANEOUS_POWER = "max_simultaneous_power"

CONF_DEVICES = "devices"
CONF_FIXED_LOADS = "fixed_loads"
CONF_NAME = "name"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_W = "power_w"
CONF_DURATION_MIN = "duration_min"
CONF_START_TIME = "start_time"
CONF_PROGRAMS = "programs"
CONF_POWER_PROFILE = "power_profile"
CONF_MINUTES = "minutes"
CONF_AUTO_DAYS = "auto_days"

# ISO weekday order (Monday first), used both as the config_flow multi-select's option values and
# to index datetime.weekday() (0=Monday) when the coordinator checks today against a program's
# auto_days.
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

CONF_IDLE_POWER_THRESHOLD = "idle_power_threshold"

CONF_PRICE_TRACKING_ENABLED = "price_tracking_enabled"
CONF_SUBSCRIPTION_PRICE_MONTHLY = "subscription_price_monthly"
CONF_TARIFF_BANDS = "tariff_bands"

CONF_FORECAST_SOURCE = "forecast_source"
FORECAST_SOURCE_ENTITY = "entity"
FORECAST_SOURCE_COMPUTED = "computed"

CONF_PV_CAPACITY_KWC = "pv_capacity_kwc"
CONF_PV_AZIMUTH = "pv_azimuth"
CONF_PV_TILT = "pv_tilt"
CONF_PV_LOSS_PCT = "pv_loss_pct"
CONF_PV_WEATHER_MODEL = "pv_weather_model"

DEFAULT_PV_LOSS_PCT = 14.0
# Verified live against Open-Meteo (2026-09-04): "best_match" is a sentinel meaning "send no
# `models` param" (Open-Meteo picks itself), not a real model id. meteofrance_arome_france_hd (the
# high-res variant) returns HTTP 200 but null for every irradiance field; the coarser
# meteofrance_arome_france actually returns data.
PV_WEATHER_MODELS = ["best_match", "meteofrance_arome_france", "ecmwf_ifs025", "gfs_seamless"]
# Irradiance forecasts don't change fast enough to justify the 15-min scheduling cycle, and this
# avoids hammering Open-Meteo's free tier.
PV_FORECAST_UPDATE_INTERVAL_MINUTES = 60

NONE_PROGRAM = "None"

DEFAULT_MAX_SIMULTANEOUS_POWER = 4000
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
DEFAULT_IDLE_POWER_THRESHOLD = 10

ATTR_END = "end"
ATTR_COVERAGE_PCT = "coverage_pct"
ATTR_DURATION_MIN = "duration_min"
ATTR_POWER_W = "power_w"
ATTR_PROFILE = "profile"
ATTR_LOCKED = "locked"
ATTR_ESTIMATED_COST = "estimated_cost"
ATTR_CURRENCY = "currency"
