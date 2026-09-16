"""Constants for OptoV integration."""

from homeassistant.const import Platform

DOMAIN = "optov"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_ENCRYPTION_KEY = "encryption_key"
CONF_DEVICE = "device"
CONF_INSTANCE = "instance"
CONF_PROXY_NAME = "proxy_name"
CONF_CATALOG = "catalog"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LANGUAGE = "language"
CONF_ENABLE_DIAGNOSTICS = "enable_diagnostics"
CONF_ENABLE_COMMISSIONING = "enable_commissioning"
CONF_ENABLE_CODING2 = "enable_coding2"
CONF_ENABLE_EXPERT = "enable_expert"
CONF_SYNC_CLOCK = "sync_clock"

DEFAULT_PORT = 6053
# Preset, not fixed -- adjustable in the integration options. 15 s matches the cadence the
# fast-moving process values want; everything slower is spread out by the scheduler, which
# sizes each cycle from measured bus time rather than from this number.
DEFAULT_SCAN_INTERVAL = 15
DEFAULT_LANGUAGE = "de"
# Off by default: the base entity set already covers what the controller shows on its own
# overview and operation menus. Diagnostics, commissioning and coding are for a debugging
# session -- switch one on, look at it, switch it off again.
DEFAULT_ENABLE_DIAGNOSTICS = False
DEFAULT_ENABLE_COMMISSIONING = False
DEFAULT_ENABLE_CODING2 = False
DEFAULT_ENABLE_EXPERT = False
# On by default: a controller clock that has drifted silently shifts every weekly programme.
DEFAULT_SYNC_CLOCK = True

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]
