"""Constants for the Solar Planner Scheduler integration."""

DOMAIN = "solar_planner_scheduler"

# Legacy, no longer settable via the form (superseded first by CONF_FORECAST_ENTITIES_SOLCAST/
# _HELIOS, then by the config_entry-based fields below): kept only because sensor.py still
# displays whatever's in an old entry's data for these two, informational, harmless.
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FORECAST_TOMORROW_ENTITY = "forecast_tomorrow_entity"
# The actual stored fields resolve_forecast_sources() reads: one config_entry_id per provider.
CONF_FORECAST_CONFIG_ENTRY_SOLCAST = "forecast_config_entry_solcast"
CONF_FORECAST_CONFIG_ENTRY_HELIOS = "forecast_config_entry_helios"
CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR = "forecast_config_entry_forecast_solar"
# A UI-only multi-select of provider keys: never stored as-is. config_flow.py's
# _resolve_provider_selection() translates it into the three concrete fields above once, at
# submission time (looking up "the" config entry for each ticked provider's real HA domain —
# there is realistically only ever one), and _base_schema() derives its prefill back from those
# same three fields when reopening the form. This keeps resolve_forecast_sources() a pure,
# hass-free lookup, resolved once per submission rather than on every coordinator read.
CONF_FORECAST_PROVIDERS_ENABLED = "forecast_providers_enabled"
FORECAST_PROVIDER_SOLCAST = "solcast"
FORECAST_PROVIDER_HELIOS = "helios_forecast"
FORECAST_PROVIDER_FORECAST_SOLAR = "forecast_solar"
FORECAST_PROVIDERS = [FORECAST_PROVIDER_SOLCAST, FORECAST_PROVIDER_HELIOS, FORECAST_PROVIDER_FORECAST_SOLAR]
# Provider key -> real HA integration domain. FORECAST_PROVIDER_HELIOS/_FORECAST_SOLAR happen to
# already equal their real domain, but FORECAST_PROVIDER_SOLCAST ("solcast") does not (the real
# domain is "solcast_solar") — confirmed against the real instance's config entries.
FORECAST_PROVIDER_DOMAINS = {
    FORECAST_PROVIDER_SOLCAST: "solcast_solar",
    FORECAST_PROVIDER_HELIOS: FORECAST_PROVIDER_HELIOS,
    FORECAST_PROVIDER_FORECAST_SOLAR: FORECAST_PROVIDER_FORECAST_SOLAR,
}
# Provider key -> its concrete stored field (see CONF_FORECAST_PROVIDERS_ENABLED above).
FORECAST_PROVIDER_CONFIG_ENTRY_FIELDS = {
    FORECAST_PROVIDER_SOLCAST: CONF_FORECAST_CONFIG_ENTRY_SOLCAST,
    FORECAST_PROVIDER_HELIOS: CONF_FORECAST_CONFIG_ENTRY_HELIOS,
    FORECAST_PROVIDER_FORECAST_SOLAR: CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR,
}
# Display labels for the config_flow multi-select; select.py extends this with the virtual
# combiner modes (Average/Min) for its own options list.
FORECAST_PROVIDER_LABELS = {
    FORECAST_PROVIDER_SOLCAST: "Solcast",
    FORECAST_PROVIDER_HELIOS: "Helios Forecast",
    FORECAST_PROVIDER_FORECAST_SOLAR: "Forecast.Solar",
}
# Virtual providers: combine every currently resolved real provider (see FORECAST_COMBINERS).
FORECAST_PROVIDER_AVERAGE = "average"
FORECAST_PROVIDER_MIN = "min"
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
CONF_PHASE_CALIBRATION_RUNS = "phase_calibration_runs"

# ISO weekday order (Monday first), used both as the config_flow multi-select's option values and
# to index datetime.weekday() (0=Monday) when the coordinator checks today against a program's
# auto_days.
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

CONF_IDLE_POWER_THRESHOLD = "idle_power_threshold"

CONF_PRICE_TRACKING_ENABLED = "price_tracking_enabled"
CONF_SUBSCRIPTION_PRICE_MONTHLY = "subscription_price_monthly"
CONF_TARIFF_BANDS = "tariff_bands"

NONE_PROGRAM = "None"

DEFAULT_MAX_SIMULTANEOUS_POWER = 4000
DEFAULT_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_IDLE_POWER_THRESHOLD = 10
DEFAULT_PHASE_CALIBRATION_RUNS = 7

ATTR_END = "end"
ATTR_COVERAGE_PCT = "coverage_pct"
ATTR_DURATION_MIN = "duration_min"
ATTR_POWER_W = "power_w"
ATTR_PROFILE = "profile"
ATTR_LOCKED = "locked"
ATTR_ESTIMATED_COST = "estimated_cost"
ATTR_CURRENCY = "currency"
