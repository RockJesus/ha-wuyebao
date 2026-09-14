"""Constants for the 物业宝 (WuyeBao) Home Assistant integration."""

DOMAIN = "wuyebao"
DOMAIN_TITLE = "物业宝"

# Configuration keys
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_BASE_URL = "base_url"
CONF_LOGIN_PATH = "login_path"
CONF_DEVICES_PATH = "devices_path"
CONF_OPEN_PATH = "open_path"
CONF_OPEN_METHOD = "open_method"
CONF_DEVICE_ID_FIELD = "device_id_field"
CONF_DEVICE_NAME_FIELD = "device_name_field"
CONF_AUTH_SCHEME = "auth_scheme"
CONF_POLL_INTERVAL = "poll_interval"

# Sensible defaults for the common app shape.
# These work for many Chinese property / door-access backends, but the real
# values must be taken from a packet capture of the user's own app.
DEFAULT_LOGIN_PATH = "/api/login"
DEFAULT_DEVICES_PATH = "/api/device/list"
DEFAULT_OPEN_PATH = "/api/device/open"
DEFAULT_OPEN_METHOD = "POST"
DEFAULT_DEVICE_ID_FIELD = "deviceId"
DEFAULT_DEVICE_NAME_FIELD = "name"
DEFAULT_AUTH_SCHEME = "Bearer"
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600

# Coordinator data keys
ATTR_DEVICES = "devices"
ATTR_LAST_OPEN = "last_open"
