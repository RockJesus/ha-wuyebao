"""Constants for the 物业宝 integration."""

DOMAIN = "propertybao"

# Configuration
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_BASE_URL = "base_url"

# Defaults
DEFAULT_BASE_URL = "https://wuye.jhws.top"
DEFAULT_CLIENT_ID = "984136508489469952"
DEFAULT_SIP_SERVER = "new-sip.jhws.top"
DEFAULT_SIP_PORT = 58583

# API Endpoints
API_TOKEN = "/api/client/anon/token"
API_REFRESH_TOKEN = "/api/client/anon/refresh_token"
API_OWNER_COMMUNITY = "/api/owner/anon/owners/community"
API_DEVICE_GATES = "/api/device/grant/gates"
API_NOTICE_LIST = "/api/notice/grant/notices"
API_ALARM_LIST = "/api/alarm/grant/alarms"
API_CALL_LIST = "/api/call/grant/calls"

# Token storage
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
SIP_ACCESS_TOKEN = "sip_access_token"
TOKEN_EXPIRES = "token_expires"
USER_ID = "user_id"
COMMUNITY_ID = "community_id"
COMMUNITY_CODE = "community_code"
USER_NAME = "user_name"
DEFAULT_OWNER = "default_owner"
