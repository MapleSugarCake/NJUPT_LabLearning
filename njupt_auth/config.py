"""Protocol addresses and defaults; no course or console configuration."""

REQUEST_TIMEOUT = (10.0, 30.0)
GET_RETRY_ATTEMPTS = 3
GET_RETRY_BACKOFF = 0.5
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
CHECK_KEY = "1629428467008"
SERVICE_URL = "http://10.22.192.38:9092/"
SSO_BASE = "https://i.njupt.edu.cn"
VPN_ORIGIN = "https://vpn.njupt.edu.cn:8443"
VPN_IDENTITY_BASE = f"{VPN_ORIGIN}/http/webvpn85b2e3dcbef5577474e4a553381b9cce"
VPN_PRELOGIN_URL = (
    f"{VPN_ORIGIN}/http/"
    "webvpnc01f87dbae47c6e4069a3da910c73ebdc209d41128f21d35b57e760d9bad4569/"
    "students/students"
)
VPN_CALLBACK = f"{VPN_ORIGIN}/enlink/api/client/callback/cas"
VPN_API_BASE = (
    f"{VPN_ORIGIN}/http/"
    "webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/"
    "jeecg-boot"
)
INTRANET_API_BASE = "http://10.22.192.38:9090/jeecg-boot"
VALIDATE_PATH = "/sys/cas/client/validateLogin"
PERMISSION_PATH = "/sys/permission/getUserPermissionByToken"
BROWSER_TIMEOUT_SECONDS = 300.0
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}
