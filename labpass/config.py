"""Static configuration for NJUPT authentication and course APIs."""

from dataclasses import dataclass

APP_NAME = "LabPass"
REQUEST_TIMEOUT = (10.0, 60.0)
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

# 新版统一身份认证（身份中台）入口，service 直接指向实验室系统前端，
# 不再经过 VPN 的 webvpn 代理（其路由 token 已失效）。
SSO_PRELOGIN_URL = f"https://i.njupt.edu.cn/cas/login?service={SERVICE_URL}"
SSO_LOGIN_URL = "https://i.njupt.edu.cn/ssoLogin/login"
SSO_AFTER_LOGIN_URL = "https://i.njupt.edu.cn/ssoLogin/index"

# 实验室系统后端（校园网直连）。
INTRANET_API_BASE = "http://10.22.192.38:9090/jeecg-boot"
INTRANET_VALIDATE_LOGIN_URL = f"{INTRANET_API_BASE}/sys/cas/client/validateLogin"


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


INTRANET_API_ENDPOINTS = build_api_endpoints(INTRANET_API_BASE, via_vpn=False)
