"""Constants for the 物业宝 integration."""

DOMAIN = "wuyebao"

# Configuration
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Defaults
DEFAULT_BASE_URL = "https://wuye.jhws.top"
# App login uses this client_id (captured from reqable). The SIP token
# endpoint uses DEFAULT_SIP_CLIENT_ID below.
DEFAULT_CLIENT_ID = "984136508489469952"

# SIP Client credentials (auto-obtain SIP token)
DEFAULT_SIP_CLIENT_ID = "984136053449428992"
DEFAULT_SIP_CLIENT_SECRET = "b1f9e5b73281b518a46fbf9e612fbcdd"

# API Endpoints
API_TOKEN = "/api/client/anon/token"
API_REFRESH_TOKEN = "/api/client/anon/refresh_token"
API_CLIENT_TOKEN = "/api/client/anon/client_token"
API_OWNER_COMMUNITY = "/api/owner/anon/owners/community"
API_GATES = "/api/device/grant/gates"
API_OWNERS = "/api/owner/grant/owners"
API_CALLS = "/api/call/{page}/{page_size}/grant/calls"
API_ALARMS = "/api/alarm/grant/alarms"

# SIP
DEFAULT_SIP_SERVER = "new-sip.jhws.top"
DEFAULT_SIP_PORT = 58583
