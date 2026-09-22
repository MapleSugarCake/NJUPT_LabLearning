"""定义只携带安全说明的认证异常层次。

本文件定义：
    AuthError：所有可报告认证失败的公共异常基类。
    AuthProtocolError：表示认证响应结构或跳转目标不符合预期。
    InvalidCredentialsError：表示账号密码为空或被身份服务明确拒绝。
    InteractionRequiredError：表示认证需要验证码或其他人工交互。
    AuthUnavailableError：表示认证服务的可用性或网络请求异常。
    AuthOutcomeUncertainError：表示一次性认证请求的处理结果无法确认。
    AuthExpiredError：表示资源或 VPN 不再接受当前认证上下文。
    BrowserUnavailableError：表示可选浏览器组件或本机 Edge 无法完成操作。
"""


# 作用：所有可报告认证失败的公共异常基类。
# 说明：继承 Exception，调用方可统一捕获；实例消息应已脱敏且适合向用户展示。
class AuthError(Exception):
    """所有可报告认证失败的公共异常基类。"""


# 作用：表示认证响应结构或跳转目标不符合预期。
# 说明：继承 AuthError；用于缺少字段、非法服务标识、重定向循环等协议问题。
class AuthProtocolError(AuthError):
    """表示认证响应结构或跳转目标不符合预期。"""


# 作用：表示账号密码为空或被身份服务明确拒绝。
# 说明：继承 AuthError；由交互层提示检查输入，不在异常处理中自动重发凭据。
class InvalidCredentialsError(AuthError):
    """表示账号密码为空或被身份服务明确拒绝。"""


# 作用：表示认证需要验证码或其他人工交互。
# 说明：继承 AuthError；交互层可据此引导用户选择浏览器登录。
class InteractionRequiredError(AuthError):
    """表示认证需要验证码或其他人工交互。"""


# 作用：表示认证服务的可用性或网络请求异常。
# 说明：继承 AuthError；说明应包含安全阶段信息，不包含原始错误页或请求内容。
class AuthUnavailableError(AuthError):
    """表示认证服务的可用性或网络请求异常。"""


# 作用：表示一次性认证请求的处理结果无法确认。
# 说明：继承 AuthError；请求可能已到达服务端，调用方应提示网页核对而非自动重放。
class AuthOutcomeUncertainError(AuthError):
    """表示一次性认证请求的处理结果无法确认。"""


# 作用：表示资源或 VPN 不再接受当前认证上下文。
# 说明：继承 AuthError；用于 HTTP 或业务认证失效以及权限探测中的登录跳转。
class AuthExpiredError(AuthError):
    """表示资源或 VPN 不再接受当前认证上下文。"""


# 作用：表示可选浏览器组件或本机 Edge 无法完成操作。
# 说明：继承 AuthError；使用固定安全说明，避免向外传播驱动异常中的认证地址及请求头。
class BrowserUnavailableError(AuthError):
    """表示可选浏览器组件或本机 Edge 无法完成操作。"""
