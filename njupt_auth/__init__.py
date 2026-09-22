"""统一导出认证入口、认证结果及网络环境类型。

公开接口：
    authenticate：按明确选择的网络执行账号密码认证。
    authenticate_in_browser：在临时 Edge 上下文中完成认证。
    import_token：仅在校园网直连条件下导入资源 Token。
    check_access：对现有资源会话执行只读权限探测。
    AuthenticationResult：持有已验证的主会话和独立会话工厂。
    NetworkEnvironment：区分校园网直连与校外 VPN 链路。
    AuthError：可向调用方报告的认证错误基类。
    AuthExpiredError：实验室资源或 VPN 认证上下文失效。
这些接口均从所属模块导入，本文件不执行认证。

本文件定义：
    __all__：认证包公开导出名称的字符串列表。
"""

from .auth import authenticate, check_access, import_token
from .browser import authenticate_in_browser
from .errors import AuthError, AuthExpiredError
from .models import AuthenticationResult, NetworkEnvironment

# 认证包公开导出名称的字符串列表。
# 约束星号导入的接口范围；名称对应上方导入的认证函数、异常和类型。
__all__ = [
    "AuthError",
    "AuthExpiredError",
    "AuthenticationResult",
    "NetworkEnvironment",
    "authenticate",
    "authenticate_in_browser",
    "check_access",
    "import_token",
]
