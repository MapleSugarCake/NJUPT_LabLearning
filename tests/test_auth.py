from collections.abc import Iterable

import pytest
import requests
from conftest import make_response

from labpass.auth import authenticate_automatically, authenticate_with_token
from labpass.config import INTRANET_API_ENDPOINTS, VPN_API_ENDPOINTS
from labpass.exceptions import AuthenticationError


class ScriptedSession(requests.Session):
    def __init__(self, responses: Iterable[requests.Response]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.was_closed = False

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        self.calls.append((method, url))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)

    def close(self) -> None:
        self.was_closed = True
        super().close()


def successful_responses(
    *, ticket_url: str = "https://example.test/?ticket=ST-123"
) -> list[requests.Response]:
    return [
        make_response(text="vpn"),
        make_response(text="sso"),
        make_response({"success": True, "code": 200}),
        make_response(text="confirmed"),
        make_response(text="cas", url="https://example.test/cas?service=ABC123"),
        make_response({"success": True, "code": 200}),
        make_response(text="ticket", url=ticket_url),
        make_response({"success": True, "code": 200, "result": {"token": "business-token"}}),
    ]


def test_automatic_authentication_completes_all_stages() -> None:
    session = ScriptedSession(successful_responses())
    session.cookies.set("JSESSIONID", "session-id")
    session.cookies.set("vpn_timestamp", "123")

    result = authenticate_automatically("20250001", "password", session_factory=lambda: session)

    assert result.session is session
    assert result.endpoints is VPN_API_ENDPOINTS
    assert result.mode == "auto"
    assert session.headers["x-access-token"] == "business-token"
    assert len(session.calls) == 8


def test_automatic_authentication_rejects_missing_session_cookie() -> None:
    session = ScriptedSession(successful_responses())

    with pytest.raises(AuthenticationError, match="JSESSIONID"):
        authenticate_automatically("20250001", "password", session_factory=lambda: session)

    assert session.was_closed


def test_automatic_authentication_rejects_missing_ticket() -> None:
    session = ScriptedSession(successful_responses(ticket_url="https://example.test/no-ticket"))
    session.cookies.set("JSESSIONID", "session-id")

    with pytest.raises(AuthenticationError, match="ticket"):
        authenticate_automatically("20250001", "password", session_factory=lambda: session)

    assert session.was_closed


def test_automatic_authentication_rejects_missing_business_token() -> None:
    responses = successful_responses()
    responses[-1] = make_response({"success": True, "code": 200, "result": {}})
    session = ScriptedSession(responses)
    session.cookies.set("JSESSIONID", "session-id")

    with pytest.raises(AuthenticationError, match="Token"):
        authenticate_automatically("20250001", "password", session_factory=lambda: session)


def test_manual_token_authentication_uses_intranet_endpoints() -> None:
    session = ScriptedSession([])

    result = authenticate_with_token(" manual-token ", session_factory=lambda: session)

    assert result.endpoints is INTRANET_API_ENDPOINTS
    assert result.mode == "token"
    assert session.headers["x-access-token"] == "manual-token"
