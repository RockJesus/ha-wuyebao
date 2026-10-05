"""Constants for the 物业宝 integration."""

DOMAIN = "wuyebao"

# Configuration
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ROOM = "room"

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
API_REPAIRS = "/api/repair/repairs"
API_INVITE_VISITORS = "/api/invitevisitor/grant/invitevisitors"
API_INVITE_VISITOR_CREATE = "/api/invitevisitor/grant"
API_FACE_INFO = "/api/face/grant/faceinfo"
API_CONTENTS = "/api/content/anon/classify/contents"

# Content types for the 小区公告/轮播 content API (classifyId = communityId)
CONTENT_TYPE_CAROUSEL = "TYPE_APP_CAROUSEL_ADS"

# SIP
DEFAULT_SIP_SERVER = "new-sip.jhws.top"
DEFAULT_SIP_PORT = 58583
