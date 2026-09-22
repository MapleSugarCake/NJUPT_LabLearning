"""提供默认阻断网络的测试夹具和模拟响应构造工具。

本文件定义：
    _SESSION_SEND：安装网络阻断前保存的 requests.Session.send 原始方法。
    block_network：为每个测试默认封锁 HTTP 发送、域名解析和 socket 连接。
    make_response：在内存中构造 requests.Response 以模拟 HTTP 返回。
    success：构造符合接口约定的成功 JSON 外层结构。
    cookie_response：构造可进入 requests 原生 Set-Cookie 解析流程的模拟响应。
    install_cookie_transport：提供保留原生 Cookie 提取流程的离线适配器安装器。
    install_transport：提供直接替换会话发送过程的离线安装器。
"""

import json
import socket
from http.client import HTTPMessage
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

# 安装网络阻断前保存的 requests.Session.send 原始方法。
# 仅在模拟适配器已安装的夹具中调用，以测试真实 Cookie 提取流程而不打开网络连接。
_SESSION_SEND = requests.Session.send


# 作用：为每个测试默认封锁 HTTP 发送、域名解析和 socket 连接。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；通过 monkeypatch 安装自动恢复的阻断函数。
# 说明：autouse 夹具默认生效，测试必须显式安装模拟传输才能触发请求流程。
@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    # 作用：在任何未模拟的网络入口调用时使测试立即失败。
    # 参数：
    #     *args：被替换网络函数收到的位置参数，仅为兼容原调用签名。
    #     **kwargs：被替换网络函数收到的关键字参数，不执行实际网络操作。
    # 返回：不返回；始终抛出 AssertionError。
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock all HTTP and network access")

    monkeypatch.setattr(requests.Session, "send", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


# 作用：在内存中构造 requests.Response 以模拟 HTTP 返回。
# 参数：
#     payload：待序列化为 JSON 的模拟响应数据；为 None 时使用文本正文。 默认值为 None。
#     status：模拟 HTTP 状态码。 默认值为 200。
#     text：没有 JSON 数据时使用的响应正文。 默认值为 ''。
#     url：模拟响应对应的请求地址。 默认值为 ''。
#     headers：附加响应头映射；省略时使用空映射。 默认值为 None。
# 返回：带状态、正文、编码和响应头的 requests.Response。
# 说明：JSON 数据使用 UTF-8 编码，正文标记为已经读取；未指定内容类型时按 JSON 或文本自动设置。
def make_response(payload=None, *, status=200, text="", url="", headers=None):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.encoding = "utf-8"
    response._content = (
        json.dumps(payload, ensure_ascii=False).encode() if payload is not None else text.encode()
    )
    response._content_consumed = True
    response.headers.update(headers or {})
    response.headers.setdefault(
        "Content-Type", "application/json" if payload is not None else "text/html"
    )
    return response


# 作用：构造符合接口约定的成功 JSON 外层结构。
# 参数：
#     result：需要嵌入的业务结果，可为字典、列表或 None。 默认值为 None。
#     code：模拟成功业务码，测试可选择 200 或 0。 默认值为 200。
# 返回：包含 success、code、message 和 result 的字典。
def success(result=None, *, code=200):
    return {"success": True, "code": code, "message": "ok", "result": result}


# 作用：构造可进入 requests 原生 Set-Cookie 解析流程的模拟响应。
# 参数：
#     payload：待序列化为 JSON 的模拟响应数据；为 None 时使用文本正文。 默认值为 None。
#     cookies：任意数量的完整 Set-Cookie 头值组成的序列。 默认值为 ()。
#     **kwargs：透传给 make_response 的状态、地址、文本或响应头选项。
# 返回：带原始头部接口和可观察 close 方法的 Response。
# 说明：模拟 raw._original_response.msg，而不创建真实 socket；便于断言关闭动作。
def cookie_response(payload=None, *, cookies=(), **kwargs):
    """构造可进入 requests 原生 Set-Cookie 解析流程的模拟响应。"""
    response = make_response(payload, **kwargs)
    message = HTTPMessage()
    for value in cookies:
        message.add_header("Set-Cookie", value)
    response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
    response.close = Mock(wraps=response.close)
    return response


# 作用：提供保留原生 Cookie 提取流程的离线适配器安装器。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：安装函数，接收模拟处理器并返回请求记录列表。
@pytest.fixture
def install_cookie_transport(monkeypatch):
    # 作用：替换会话与适配器发送入口并开始记录调用。
    # 参数：
    #     handler：处理模拟请求并返回响应的可调用对象。
    # 返回：随请求不断追加会话、准备请求和传输参数的列表。
    # 说明：会话发送仍调用保存的原方法，最底层适配器由 handler 提供响应。
    def install(handler):
        calls = []

        # 作用：记录会话发送后进入原生 requests 响应处理流程。
        # 参数：
        #     session：触发发送的 requests 会话，供记录会话归属及隔离性。
        #     request：已经准备好的请求对象，包含待验证的方法、地址、请求头和请求体。
        #     **kwargs：请求传输选项，例如超时和是否允许跳转；用于记录及离线断言。
        # 返回：由原生会话方法处理过 Cookie 的模拟响应。
        # 说明：底层 HTTPAdapter.send 已被替换，调用不会打开真实网络连接。
        def send(session, request, **kwargs):
            calls.append((session, request, kwargs))
            return _SESSION_SEND(session, request, **kwargs)

        # 作用：把底层发送委托给模拟处理器并补全响应请求信息。
        # 参数：
        #     adapter：被替换发送方法所属的适配器，仅用于兼容实例方法调用。
        #     request：已经准备好的请求对象，包含待验证的方法、地址、请求头和请求体。
        #     **kwargs：请求传输选项，例如超时和是否允许跳转；用于记录及离线断言。
        # 返回：handler 返回并补全 request、url 的响应对象。
        def adapter_send(adapter, request, **kwargs):
            response = handler(request, kwargs)
            response.request = request
            if not response.url:
                response.url = request.url
            return response

        monkeypatch.setattr(requests.Session, "send", send)
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", adapter_send)
        return calls

    return install


# 作用：提供直接替换会话发送过程的离线安装器。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：接收 handler 的安装函数，可记录并模拟每次请求。
@pytest.fixture
def install_transport(monkeypatch):
    # 作用：安装简单模拟发送函数并创建共享调用记录。
    # 参数：
    #     handler：处理模拟请求并返回响应的可调用对象。
    # 返回：按发送顺序追加请求信息的列表。
    # 说明：适用于无需真实 Set-Cookie 解析的业务及传输测试。
    def install(handler):
        calls = []

        # 作用：记录请求并将其交给提供的模拟服务处理。
        # 参数：
        #     session：触发发送的 requests 会话，供记录会话归属及隔离性。
        #     request：已经准备好的请求对象，包含待验证的方法、地址、请求头和请求体。
        #     **kwargs：请求传输选项，例如超时和是否允许跳转；用于记录及离线断言。
        # 返回：补齐准备请求及默认地址的模拟响应。
        # 说明：handler 的异常直接传播，用于测试超时、连接失败或用户中断。
        def send(session, request, **kwargs):
            calls.append((session, request, kwargs))
            response = handler(session, request, kwargs)
            response.request = request
            if not response.url:
                response.url = request.url
            return response

        monkeypatch.setattr(requests.Session, "send", send)
        return calls

    return install
