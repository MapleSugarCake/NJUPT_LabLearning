"""Offline fixtures. No test may contact a school service or use real secrets."""

import json
import socket
from http.client import HTTPMessage
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

_SESSION_SEND = requests.Session.send


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock all HTTP and network access")

    monkeypatch.setattr(requests.Session, "send", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


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


def success(result=None, *, code=200):
    return {"success": True, "code": code, "message": "ok", "result": result}


def cookie_response(payload=None, *, cookies=(), **kwargs):
    """Exercise requests' real Set-Cookie extraction without opening a socket."""
    response = make_response(payload, **kwargs)
    message = HTTPMessage()
    for value in cookies:
        message.add_header("Set-Cookie", value)
    response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
    response.close = Mock(wraps=response.close)
    return response


@pytest.fixture
def install_cookie_transport(monkeypatch):
    def install(handler):
        calls = []

        def send(session, request, **kwargs):
            calls.append((session, request, kwargs))
            return _SESSION_SEND(session, request, **kwargs)

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


@pytest.fixture
def install_transport(monkeypatch):
    def install(handler):
        calls = []

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
