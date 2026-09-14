"""Constants for the 物业宝（家和云联） Home Assistant integration.

This integration is tailored to the JHCloud (深圳家和云联网络有限公司)
"物业宝（业主）" app. The API contract was reverse-engineered from the
official Android APK (WuYeBao-2025-09-15-1.1.1.51):

  * API root ......... https://wuye.jhws.top/
  * Login ............ POST /api/client/anon/token
                      headers: client_id: <client id>
                      body:    {"username": <phone>, "password": <pwd>}
  * Refresh token .... GET /api/client/anon/refresh_token?refreshToken=...
  * Gate list ........ GET /api/device/grant/gates   (Authorization: Bearer)
  * Owner list ....... GET /api/owner/grant/owners   (Authorization: Bearer)
"""

DOMAIN = "wuyebao"
DOMAIN_TITLE = "物业宝（家和云联）"

# --------------------------------------------------------------------------
# Configuration keys
# --------------------------------------------------------------------------
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_BASE_URL = "base_url"
CONF_CLIENT_ID = "client_id"
CONF_OPEN_PATH = "open_path"
CONF_OPEN_METHOD = "open_method"
CONF_POLL_INTERVAL = "poll_interval"

# --------------------------------------------------------------------------
# Verified JHCloud API paths
# --------------------------------------------------------------------------
API_LOGIN_PATH = "api/client/anon/token"
API_REFRESH_PATH = "api/client/anon/refresh_token"
API_OWNERS_PATH = "api/owner/grant/owners"
API_GATES_PATH = "api/device/grant/gates"

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------
# A client_id is mandatory on every request ("请求头中无client_id信息!").
# The value below comes from the official app binary (libapp.so); several
# known-good values exist, this one is the app's primary id. It can be
# changed in the integration options if a different client id is required.
DEFAULT_BASE_URL = "https://wuye.jhws.top/"
DEFAULT_CLIENT_ID = "984136053449428992"

# The app itself opens doors through the SIP intercom (云对讲) core, so there
# is no public HTTP "unlock" path inside the app binary. This integration
# ships the most likely REST-style path as a default and lets you configure
# any other path ({gateId} is replaced with the gate id) if your server
# exposes a different one.
DEFAULT_OPEN_PATH = "api/device/grant/gates/{gateId}/unlock"
DEFAULT_OPEN_METHOD = "POST"

DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600

# --------------------------------------------------------------------------
# Coordinator data keys
# --------------------------------------------------------------------------
ATTR_GATES = "gates"
ATTR_OWNER = "owner"
ATTR_LAST_OPEN = "last_open"

# Gate identity / name field candidates (in priority order).
GATE_ID_FIELD_CANDIDATES = (
    "id",
    "gateId",
    "deviceId",
    "doorId",
    "serialNo",
    "sn",
    "deviceNumber",
    "code",
)
GATE_NAME_FIELD_CANDIDATES = (
    "name",
    "gateName",
    "deviceName",
    "doorName",
    "alias",
    "title",
)
