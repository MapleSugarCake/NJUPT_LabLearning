"""Static configuration for NJUPT authentication and course APIs."""

from dataclasses import dataclass

APP_NAME = "LabPass"
REQUEST_TIMEOUT = (10.0, 30.0)
GET_RETRY_ATTEMPTS = 3
GET_RETRY_BACKOFF = 0.5
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
DEFAULT_WORKERS = 4
MAX_WORKERS = 4

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

CHECK_KEY = "1629428467008"
APP_ID = "1442771163964026882"
SERVICE_URL = "http://10.22.192.38:9092/"

VPN_PRELOGIN_URL = (
    "https://vpn.njupt.edu.cn:8443/http/"
    "webvpnc01f87dbae47c6e4069a3da910c73ebdc209d41128f21d35b57e760d9bad4569/"
    "students/students"
)
SSO_PRELOGIN_URL = (
    "https://i.njupt.edu.cn/cas/login?"
    "service=https://vpn.njupt.edu.cn:8443/enlink/api/client/callback/cas"
)
SSO_LOGIN_URL = "https://i.njupt.edu.cn/ssoLogin/login"
SSO_AFTER_LOGIN_URL = "https://i.njupt.edu.cn/ssoLogin/index"
VPN_CAS_PRELOGIN_URL = (
    "https://vpn.njupt.edu.cn:8443/http/"
    "webvpn85b2e3dcbef5577474e4a553381b9cce/cas/login?"
    "service=http%3A%2F%2F10.22.192.38%3A9092%2F"
)
VPN_CAS_LOGIN_URL = (
    "https://vpn.njupt.edu.cn:8443/http/"
    "webvpn85b2e3dcbef5577474e4a553381b9cce/ssoLogin/login?enlink-vpn"
)
VPN_CAS_AFTER_LOGIN_URL = (
    "https://vpn.njupt.edu.cn:8443/http/webvpn85b2e3dcbef5577474e4a553381b9cce/ssoLogin/index"
)
VPN_VALIDATE_LOGIN_URL = (
    "https://vpn.njupt.edu.cn:8443/http/"
    "webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/"
    "jeecg-boot/sys/cas/client/validateLogin"
)

VPN_API_BASE = (
    "https://vpn.njupt.edu.cn:8443/http/"
    "webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/"
    "jeecg-boot"
)
INTRANET_API_BASE = "http://10.22.192.38:9090/jeecg-boot"


@dataclass(frozen=True, slots=True)
class ApiEndpoints:
    """Resolved business endpoints for one access mode."""

    courses: str
    questions: str
    submit_answer: str
    finish_course: str
    requires_vpn_timestamp: bool


def build_api_endpoints(base_url: str, *, via_vpn: bool) -> ApiEndpoints:
    suffix = "?enlink-vpn" if via_vpn else ""
    course_source = f"{base_url}/jcedutec/courseSource"
    return ApiEndpoints(
        courses=f"{course_source}/myCourseList",
        questions=f"{course_source}/queryCourseQuestionRelaByMainId",
        submit_answer=f"{course_source}/submitAnswer{suffix}",
        finish_course=f"{course_source}/finish{suffix}",
        requires_vpn_timestamp=via_vpn,
    )


VPN_API_ENDPOINTS = build_api_endpoints(VPN_API_BASE, via_vpn=True)
INTRANET_API_ENDPOINTS = build_api_endpoints(INTRANET_API_BASE, via_vpn=False)
