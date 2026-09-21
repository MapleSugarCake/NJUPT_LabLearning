"""Offline fixtures. No test may contact a school service or use real secrets."""

import json
import socket

import pytest
import requests


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
