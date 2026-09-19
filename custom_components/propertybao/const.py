"""Constants for the 物业宝 integration."""

DOMAIN = "propertybao"

# Configuration
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Defaults
DEFAULT_BASE_URL = "https://wuye.jhws.top"
DEFAULT_CLIENT_ID = "984136053449428992"

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

# SIP
DEFAULT_SIP_SERVER = "new-sip.jhws.top"
DEFAULT_SIP_PORT = 58583
