"""Constants for the Edulog Parent Portal Lite HACS integration.

Product note: this integration drives **Edulog Parent Portal Lite**
(`com.karrostech.parentportallite.android`), a scoped-down companion app.
It is NOT the full "Edulog Parent Portal" — that is a different product with
a different account/auth flow, and none of its endpoints are exercised here.
"""

DOMAIN = "edulog_lite"

CONF_PASSWORD = "password"
CONF_TENANT_ID = "tenant_id"
CONF_TENANT_NAME = "tenant_name"
CONF_TENANT_CODE = "tenant_code"
CONF_VEHICLE_IDS = "vehicle_ids"

CONF_POLL_INTERVAL = "poll_interval"
CONF_THRESHOLD_M = "threshold_m"
CONF_WATCH_LAT = "watch_lat"
CONF_WATCH_LNG = "watch_lng"

DEFAULT_POLL_INTERVAL = 30
DEFAULT_THRESHOLD_M = 500

#: GPS fixes older than this are treated as "no location" (end of day,
#: off-route, or a feed gap) rather than as a valid position.
STALE_AFTER_S = 1200

POLL_INTERVAL_CHOICES = (30, 60, 120)

# --- App constants (extracted from the APK's BuildConfig.kt; see FLOW.md) ----
API = "https://parentportallitegateway-p01-prod.usw1.karrostech.net"  # env "prod"
CLIENT_ID = "c62ac14ec31ffdd04d98268aeb6ec3b5244b1bcf"  # ApiKey header (app gateway)
CLIENT_SECRET = "138abf2e93cf885379c77054e15673e73fe70b5117f42ca498a42c3955e1658b"

KEYCLOAK_TOKEN_URL = (
    "https://accounts.karrostech.net/auth/realms/Edulog/protocol/openid-connect/token"
)
KEYCLOAK_CLIENT_ID = "parent-portal-lite"
KEYCLOAK_CLIENT_SECRET = "ca78fc18-ad06-461d-a0fe-1af98446cad7"

APP_TYPE = "Parent Portal Lite"  # application_type (legacy signin path)
FULL_ACCESS = "Full access"  # authTokenType (legacy signin path)
REGCODE_SUB = "REGISTRATIONCODE_SUBSCRIPTION"  # access-request type: district subscriptions
BUS_FOLLOW = "BUS_FOLLOW_REQUEST"  # access-request type: followed buses
USER_AGENT = (
    "Edulog Parent Portal Lite/1.15.9 (com.karrostech.parentportallite.android; build:2160901)"
)
