"""Constants for the Solar Planner Scheduler integration."""

DOMAIN = "solar_planner_scheduler"

CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FORECAST_TOMORROW_ENTITY = "forecast_tomorrow_entity"
CONF_SURPLUS_ENTITY = "surplus_entity"
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
CONF_SELECTED_PROGRAM = "selected_program"

CONF_HISTORY_LOOKBACK_DAYS = "history_lookback_days"
CONF_IDLE_POWER_THRESHOLD = "idle_power_threshold"
CONF_DURATION_TOLERANCE_PERCENT = "duration_tolerance_percent"
CONF_RUN_GAP_TOLERANCE_MINUTES = "run_gap_tolerance_minutes"

NONE_PROGRAM = "None"

DEFAULT_MAX_SIMULTANEOUS_POWER = 4000
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
DEFAULT_HISTORY_LOOKBACK_DAYS = 30
DEFAULT_IDLE_POWER_THRESHOLD = 10
DEFAULT_DURATION_TOLERANCE_PERCENT = 20
DEFAULT_RUN_GAP_TOLERANCE_MINUTES = 5

ATTR_COVERAGE_PCT = "coverage_pct"
ATTR_DURATION_MIN = "duration_min"
ATTR_POWER_W = "power_w"
ATTR_APPROXIMATE = "approximate"
