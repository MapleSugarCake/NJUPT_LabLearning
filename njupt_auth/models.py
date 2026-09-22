"""定义显式网络环境和拥有资源会话的认证结果。

本文件定义：
    NetworkEnvironment：用字符串枚举区分认证所使用的网络链路。
    NetworkEnvironment.INTRANET：校园网直连环境的枚举成员。
    NetworkEnvironment.EXTRANET：校外 VPN 环境的枚举成员。
    AuthenticationResult：交接已验证主会话、资源基址和独立会话工厂。
    AuthenticationResult.__enter__：进入认证结果上下文并提供当前结果。
    AuthenticationResult.__exit__：退出认证结果上下文时关闭会话及工厂。
    AuthenticationResult.close：按顺序关闭主会话及资源会话工厂。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Self

import requests

if TYPE_CHECKING:
    from .transport import ResourceSessionFactory


# 作用：用字符串枚举区分认证所使用的网络链路。
# 说明：继承 StrEnum，调用方应在登录前明确选择；不通过超时推断环境。
class NetworkEnvironment(StrEnum):
    # 校园网直连环境的枚举成员。
    # 对应值 intranet，用于选择内网资源地址和直接 CAS 链路。
    INTRANET = "intranet"
    # 校外 VPN 环境的枚举成员。
    # 对应值 extranet，用于选择 VPN 入口及映射资源地址。
    EXTRANET = "extranet"


# 作用：交接已验证主会话、资源基址和独立会话工厂。
# 说明：带 slots 的数据类；会话和工厂不出现在自动生成的表示文本中。
# 说明：支持上下文管理；应在全部课程线程结束后关闭，以释放主会话和认证快照。
@dataclass(slots=True)
class AuthenticationResult:
    """交接已验证主会话、资源基址和独立会话工厂。"""

    session: requests.Session = field(repr=False)
    api_base_url: str
    session_factory: "ResourceSessionFactory" = field(repr=False)

    # 作用：进入认证结果上下文并提供当前结果。
    # 参数：
    #     self：当前实例。
    # 返回：当前 AuthenticationResult 实例。
    def __enter__(self) -> Self:
        return self

    # 作用：退出认证结果上下文时关闭会话及工厂。
    # 参数：
    #     self：当前实例。
    #     *_：上下文协议传入的异常类型、异常对象和堆栈，本方法不单独处理。
    # 返回：None，不抑制上下文中的异常。
    def __exit__(self, *_: object) -> None:
        self.close()

    # 作用：按顺序关闭主会话及资源会话工厂。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    # 说明：调用前需等待课程线程结束；也支持仅做认证或权限验证的独立使用场景。
    def close(self) -> None:
        """按顺序关闭主会话及资源会话工厂。"""
        self.session.close()
        self.session_factory.close()
