"""实现显式 GET 重试、资源地址限制及独立会话工厂。

本文件定义：
    logger：传输层重试进度的模块日志记录器。
    cookies_for_url：复制目标地址实际可发送的 Cookie。
    cookie_value：按目标地址及路径优先级查找同名 Cookie。
    _retry_delay：计算下一次 GET 尝试前的有限等待时长。
    HttpSession：提供受控 GET 重试并禁止隐式跳转的基础会话。
    HttpSession.__init__：初始化默认请求头、无重试适配器及会话状态。
    HttpSession.request：按允许的方法和故障类型发送请求并执行有限重试。
    HttpSession.close：关闭连接适配器并清空会话 Cookie。
    ResourceSession：将资源认证头限制在批准的 API 范围内。
    ResourceSession.__init__：保存资源访问范围、Token 和 VPN 模式。
    ResourceSession.request：校验资源目标后添加认证头及适用的 VPN 参数。
    ResourceSession.close：关闭资源会话并释放实例持有的 Token 引用。
    ResourceSessionFactory：从本轮认证快照创建相互独立的资源会话。
    ResourceSessionFactory.__init__：复制资源主机相关 Cookie 及附加请求头作为认证快照。
    ResourceSessionFactory.__call__：在锁内根据快照创建新的资源会话。
    ResourceSessionFactory.close：禁止继续创建会话并清空认证快照。
"""

import copy
import logging
import posixpath
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar, get_cookie_header

from .config import (
    DEFAULT_HEADERS,
    GET_RETRY_ATTEMPTS,
    GET_RETRY_BACKOFF,
    REQUEST_TIMEOUT,
    RETRY_STATUS_CODES,
)
from .redaction import Redactor

# 传输层重试进度的模块日志记录器。
# 仅报告重试次数，由 CLI 的共享脱敏处理器控制输出。
logger = logging.getLogger(__name__)


# 作用：复制目标地址实际可发送的 Cookie。
# 参数：
#     cookies：源 CookieJar，读取或复制时保留 Cookie 的作用域和安全属性。
#     url：本次请求的目标地址。
# 返回：全新 CookieJar，条目为独立深拷贝。
# 说明：通过 requests 的 Cookie 匹配规则检查域、路径和安全属性，不把 Cookie 简化为名称和值。
def cookies_for_url(cookies: RequestsCookieJar, url: str) -> RequestsCookieJar:
    """复制目标地址实际可发送的 Cookie。"""
    selected = RequestsCookieJar()
    prepared = requests.Request("GET", url).prepare()
    for cookie in cookies:
        candidate = RequestsCookieJar()
        candidate.set_cookie(copy.deepcopy(cookie))
        if get_cookie_header(candidate, prepared):
            selected.set_cookie(copy.deepcopy(cookie))
    return selected


# 作用：按目标地址及路径优先级查找同名 Cookie。
# 参数：
#     cookies：源 CookieJar，读取或复制时保留 Cookie 的作用域和安全属性。
#     name：待查找的 Cookie 名称。
#     url：本次请求的目标地址。
# 返回：最长匹配路径的 Cookie 值；不存在时返回 None。
def cookie_value(cookies: RequestsCookieJar, name: str, url: str) -> str | None:
    """按目标地址及路径优先级查找同名 Cookie。"""
    matches = sorted(
        (cookie for cookie in cookies_for_url(cookies, url) if cookie.name == name),
        key=lambda cookie: len(cookie.path),
        reverse=True,
    )
    return matches[0].value if matches else None


# 作用：计算下一次 GET 尝试前的有限等待时长。
# 参数：
#     response：可重试请求的响应；连接失败时为 None。
#     attempt：从零开始的当前尝试索引，用于指数退避。
# 返回：范围为 0–30 秒的浮点等待时间。
# 说明：优先读取 Retry-After 的秒数或 HTTP 日期；无有效值时使用指数退避。
# 说明：仅计算等待时间，不决定哪些状态允许重试。
def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    delay = GET_RETRY_BACKOFF * 2**attempt
    if response is not None:
        value = response.headers.get("Retry-After", "")
        try:
            delay = float(value)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                delay = (deadline - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, min(delay, 30.0))


# 作用：提供受控 GET 重试并禁止隐式跳转的基础会话。
# 说明：继承 requests.Session；连接适配器的自动重试关闭，重试由 request 显式管理。
# 说明：默认连接和读取超时为 10/30 秒；POST 与标记为一次性的 GET 只有一次尝试。
class HttpSession(requests.Session):
    """提供受控 GET 重试并禁止隐式跳转的基础会话。"""

    # 作用：初始化默认请求头、无重试适配器及会话状态。
    # 参数：
    #     self：当前实例。
    #     redactor：可共享的脱敏上下文；未传入时为该会话新建。 默认值为 None。
    #     sleep：重试前等待的可调用对象，可注入不实际等待的测试实现。 默认值为 time.sleep。
    # 返回：无返回值（None）。
    # 说明：关闭父类初始适配器，再分别安装 HTTP 和 HTTPS 的独立适配器。
    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__()
        self.redactor = redactor if redactor is not None else Redactor()
        self._sleep = sleep
        self.closed = False
        self.headers.update(DEFAULT_HEADERS)
        for adapter in self.adapters.values():
            adapter.close()
        self.mount("http://", HTTPAdapter(max_retries=0))
        self.mount("https://", HTTPAdapter(max_retries=0))

    # 作用：按允许的方法和故障类型发送请求并执行有限重试。
    # 参数：
    #     self：当前实例。
    #     method：HTTP 请求方法，转换为大写后判断是否允许 GET 重试。
    #     url：本次请求的目标地址。
    #     retry_get：是否允许 GET 重试；设为 False 可保护票据消费等一次性请求。 默认值为 True。
    #     **kwargs：传递给 requests 的请求选项，如请求头、查询参数、请求体及超时。
    # 返回：最终响应，由调用方负责关闭。
    # 说明：允许重试的 GET 最多尝试三次，仅覆盖连接、读取故障及 429、500、502、503、504。
    #     证书错误直接抛出；收到其他 HTTP 状态时立即返回响应。
    # 说明：业务 401/403 响应立即交给上层处理；POST 不重试，所有方法均禁止自动重定向。
    # 说明：再次尝试前关闭旧响应；已关闭会话或最后一次传输失败时抛出 requests 异常。
    def request(
        self, method: str, url: str, *, retry_get: bool = True, **kwargs: Any
    ) -> requests.Response:
        if self.closed:
            raise requests.RequestException("会话已关闭")
        method = method.upper()
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = kwargs.get("timeout") or REQUEST_TIMEOUT
        attempts = GET_RETRY_ATTEMPTS if method == "GET" and retry_get else 1
        for attempt in range(attempts):
            response = None
            try:
                response = super().request(method, url, **kwargs)
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                if isinstance(exc, requests.exceptions.SSLError) or attempt + 1 == attempts:
                    raise
            else:
                self.redactor.remember(*(cookie.value for cookie in self.cookies))
                if response.status_code in RETRY_STATUS_CODES:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict) and str(payload.get("code")) in {"401", "403"}:
                        return response
                if response.status_code not in RETRY_STATUS_CODES or attempt + 1 == attempts:
                    return response
            delay = _retry_delay(response, attempt)
            if response is not None:
                response.close()
            logger.debug("GET 暂时失败，将进行第 %d/%d 次尝试", attempt + 2, attempts)
            self._sleep(delay)
        raise AssertionError("unreachable")

    # 作用：关闭连接适配器并清空会话 Cookie。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    # 说明：通过关闭标记保证重复调用安全，后续请求会被拒绝。
    def close(self) -> None:
        if not self.closed:
            super().close()
            self.cookies.clear()
            self.closed = True


# 作用：将资源认证头限制在批准的 API 范围内。
# 说明：继承 HttpSession，在父类超时及重试约束之上执行地址检查和 VPN 参数处理。
# 说明：Token 单独保存在实例中，仅在获准请求上附加，不作为默认头泄露到其他地址。
class ResourceSession(HttpSession):
    """将资源认证头限制在批准的 API 范围内。"""

    # 作用：保存资源访问范围、Token 和 VPN 模式。
    # 参数：
    #     self：当前实例。
    #     api_base_url：认证完成后允许访问的实验室 API 基址。
    #     token：实验室资源 Token，仅向匹配的资源 API 发送。
    #     via_vpn：是否经 VPN 访问资源，决定附加时间戳和网关查询参数。
    #     redactor：共享的运行级脱敏上下文，用于登记收到的认证材料。
    # 返回：无返回值（None）。
    # 说明：规范化基址尾斜杠，并将 Token 登记到共享脱敏上下文。
    def __init__(self, api_base_url: str, token: str, *, via_vpn: bool, redactor: Redactor) -> None:
        super().__init__(redactor=redactor)
        self.api_base_url = api_base_url.rstrip("/")
        self._token = token
        self._via_vpn = via_vpn
        self.redactor.remember(token)

    # 作用：校验资源目标后添加认证头及适用的 VPN 参数。
    # 参数：
    #     self：当前实例。
    #     method：HTTP 请求方法，按大写形式决定重试和 VPN 参数处理。
    #     url：本次请求的目标地址。
    #     **kwargs：传递给 requests 的请求选项，如请求头、查询参数、请求体及超时。
    # 返回：父类传输流程返回的响应。
    # 说明：目标须同协议、同主机且位于 API 子路径；片段、路径穿越或可疑编码抛出 InvalidURL。
    # 说明：VPN GET 从匹配 Cookie 取得 _t，VPN POST 添加 enlink-vpn；之后沿用父类重试约束。
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        base, target = urlsplit(self.api_base_url), urlsplit(url)
        path = unquote(target.path)
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or target.fragment
            or "\\" in path
            or not posixpath.normpath(path).startswith(base.path + "/")
            or "%" in path
        ):
            raise requests.exceptions.InvalidURL("拒绝向认证资源范围之外发送请求")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Access-Token"] = self._token
        kwargs["headers"] = headers
        if self._via_vpn:
            if method.upper() == "GET":
                params = dict(kwargs.pop("params", {}) or {})
                timestamp = cookie_value(self.cookies, "vpn_timestamp", url)
                if timestamp:
                    params.setdefault("_t", timestamp)
                kwargs["params"] = params
            elif method.upper() == "POST":
                query = target.query
                if "enlink-vpn" not in query.split("&"):
                    query = f"{query}&enlink-vpn" if query else "enlink-vpn"
                url = urlunsplit(target._replace(query=query))
        return super().request(method, url, **kwargs)

    # 作用：关闭资源会话并释放实例持有的 Token 引用。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    # 说明：父类负责适配器和 Cookie 清理，Token 引用清空不代表内存擦除。
    def close(self) -> None:
        super().close()
        self._token = ""


# 作用：从本轮认证快照创建相互独立的资源会话。
# 说明：保存 Token、Cookie 和附加请求头的内存快照，使用锁协调创建与关闭。
# 说明：各会话拥有独立 CookieJar、Cookie 对象和适配器，仅共享运行级脱敏上下文。
class ResourceSessionFactory:
    """从本轮认证快照创建相互独立的资源会话。"""

    # 作用：复制资源主机相关 Cookie 及附加请求头作为认证快照。
    # 参数：
    #     self：当前实例。
    #     api_base_url：认证完成后允许访问的实验室 API 基址。
    #     token：实验室资源 Token，仅向匹配的资源 API 发送。
    #     cookies：源 CookieJar，读取或复制时保留 Cookie 的作用域和安全属性。
    #     via_vpn：是否经 VPN 访问资源，决定附加时间戳和网关查询参数。
    #     redactor：共享的运行级脱敏上下文，用于登记收到的认证材料。
    #     headers：需带入新会话的附加请求头；省略时不额外添加。 默认值为 None。
    # 返回：无返回值（None）。
    # 说明：保留该主机下不同路径的 Cookie，避免仅在基址匹配时丢弃 API 子路径材料。
    # 说明：登记 Token 和 Cookie 值供日志脱敏，源 Cookie 对象不由工厂持有。
    def __init__(
        self,
        api_base_url: str,
        token: str,
        cookies: RequestsCookieJar,
        *,
        via_vpn: bool,
        redactor: Redactor,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.api_base_url = api_base_url
        self._token = token
        # 保留目标主机下的 Cookie，包括仅适用于 API 子路径的条目。
        host = urlsplit(api_base_url).hostname or ""
        self._cookies = RequestsCookieJar()
        for cookie in cookies:
            domain = cookie.domain.lstrip(".")
            if host == domain or (cookie.domain_initial_dot and host.endswith("." + domain)):
                self._cookies.set_cookie(copy.deepcopy(cookie))
        self._headers = dict(headers or {})
        self._via_vpn = via_vpn
        self._redactor = redactor
        self._lock = threading.Lock()
        self._closed = False
        redactor.remember(token, *(cookie.value for cookie in self._cookies))

    # 作用：在锁内根据快照创建新的资源会话。
    # 参数：
    #     self：当前实例。
    # 返回：拥有独立 CookieJar 和适配器的 ResourceSession。
    # 说明：工厂已关闭时抛出 RuntimeError；新会话由调用方或对应课程任务关闭。
    def __call__(self) -> ResourceSession:
        with self._lock:
            if self._closed:
                raise RuntimeError("认证会话工厂已关闭")
            session = ResourceSession(
                self.api_base_url, self._token, via_vpn=self._via_vpn, redactor=self._redactor
            )
            session.headers.update(self._headers)
            session.cookies = copy.deepcopy(self._cookies)
            return session

    # 作用：禁止继续创建会话并清空认证快照。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    # 说明：应在拥有者等待全部课程线程结束后调用；本方法不负责关闭已经交出的会话。
    def close(self) -> None:
        """禁止继续创建会话并清空认证快照。"""
        with self._lock:
            self._closed = True
            self._token = ""
            self._cookies.clear()
            self._headers.clear()
