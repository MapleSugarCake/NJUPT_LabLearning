"""Redirect progress, one-use gateway credentials and bounded offline authentication."""

import io
import logging
from urllib.parse import quote, urlencode, urlsplit

import pytest
import requests
from conftest import cookie_response

from labpass_cli.logging_utils import configure_logging
from njupt_auth.auth import _follow
from njupt_auth.config import (
    SERVICE_URL,
    SSO_BASE,
    VALIDATE_PATH,
    VPN_API_BASE,
    VPN_AUTHORIZATION,
    VPN_CALLBACK,
    VPN_IDENTITY_BASE,
    VPN_ORIGIN,
    VPN_PRELOGIN_URL,
)
from njupt_auth.errors import AuthError, AuthProtocolError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession


def authorization(token="fake+authorization/key", target=VPN_PRELOGIN_URL):
    return VPN_AUTHORIZATION + "?" + urlencode({"redirect_url": target, "entoken": token})


def redirect(target, *cookies, status=302):
    return cookie_response(status=status, headers={"Location": target}, cookies=cookies)


@pytest.mark.parametrize(
    "cookie",
    [None, "unrelated=fake; Domain=other.test; Path=/", "unrelated=fake; Path=/unrelated; Secure"],
)
def test_cookies_not_sent_to_destination_do_not_count_as_progress(install_cookie_transport, cookie):
    response = redirect(VPN_PRELOGIN_URL, *((cookie,) if cookie else ()))
    calls = install_cookie_transport(lambda *args: response)
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == 1 and response.close.called


@pytest.mark.parametrize("status", [301, 303, 307, 308])
def test_only_verified_gateway_response_advances_phase(install_cookie_transport, status):
    responses = iter([redirect(authorization()), redirect(VPN_PRELOGIN_URL, status=status)])
    calls = install_cookie_transport(lambda *args: next(responses))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == 2


@pytest.mark.parametrize("use_authorization", [False, True])
def test_unverified_return_does_not_advance_phase(install_cookie_transport, use_authorization):
    other = VPN_ORIGIN + "/enlink/other"
    responses = [redirect(other), redirect(VPN_PRELOGIN_URL)]
    if use_authorization:
        responses.insert(0, redirect(authorization()))
    remaining = iter(responses)
    calls = install_cookie_transport(lambda *args: next(remaining))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == len(responses)


@pytest.mark.parametrize("second_authorization", [False, True])
def test_gateway_progress_occurs_once_and_subsequent_loops_fail(
    install_cookie_transport, second_authorization
):
    repeated = authorization("fake-second-key") if second_authorization else VPN_PRELOGIN_URL
    responses = iter(
        [
            redirect(authorization()),
            redirect(VPN_PRELOGIN_URL),
            redirect(repeated),
            redirect(VPN_PRELOGIN_URL),
        ]
    )
    calls = install_cookie_transport(lambda *args: next(responses))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == (4 if second_authorization else 3)


@pytest.mark.parametrize("gateway_progress", [False, True])
def test_cookie_and_gateway_progress_do_not_reset_hop_budget(
    install_cookie_transport, gateway_progress
):
    def handler(*args):
        if gateway_progress and len(calls) == 1:
            return redirect(authorization())
        return redirect(VPN_PRELOGIN_URL, f"gateway=fake-{len(calls)}; Path=/; Secure")

    calls = install_cookie_transport(handler)
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="10"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == 11


@pytest.mark.parametrize(
    "params",
    [
        "redirect_url=fake",
        "entoken=&redirect_url=fake",
        "entoken=+&redirect_url=fake",
        "entoken=fake&entoken=&redirect_url=fake",
        "entoken=fake&entoken=other&redirect_url=fake",
        "entoken=fake",
        "entoken=fake&redirect_url=",
        "entoken=fake&redirect_url=one&redirect_url=two",
    ],
)
def test_gateway_parameters_must_be_unambiguous(install_cookie_transport, params):
    calls = install_cookie_transport(lambda *args: pytest.fail("Invalid authorization requested"))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="授权地址"):
        _follow(session, VPN_AUTHORIZATION + "?" + params, "synthetic", via_vpn=True)
    assert not calls


@pytest.mark.parametrize(
    "replayed",
    [
        authorization(),
        authorization(target=VPN_ORIGIN + "/enlink/changed"),
        VPN_AUTHORIZATION + "?entoken=fake%2bauthorization%2fkey&redirect_url=fake&extra=fake",
    ],
)
def test_gateway_credential_cannot_replay_across_stages_or_url_changes(
    install_cookie_transport, replayed
):
    calls = install_cookie_transport(lambda *args: cookie_response(text="done"))
    consumed = set()
    with HttpSession() as session:
        _follow(session, authorization(), "first", via_vpn=True, consumed=consumed).close()
        session.cookies.set("gateway", "fake-new", domain="vpn.njupt.edu.cn", path="/")
        with pytest.raises(AuthProtocolError, match="循环"):
            _follow(session, replayed, "second", via_vpn=True, consumed=consumed)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        SSO_BASE + "/cas/granting",
        VPN_IDENTITY_BASE + "/cas/granting",
        VPN_CALLBACK + "?ticket=fake-ticket",
        VPN_API_BASE + VALIDATE_PATH + "?ticket=fake-ticket",
        authorization(),
    ],
)
@pytest.mark.parametrize("fault", ["timeout", "connection", 429, 500, 502, 503, 504])
def test_single_use_authentication_requests_never_retry(install_cookie_transport, url, fault):
    def handler(*args):
        if fault == "timeout":
            raise requests.ReadTimeout("synthetic")
        if fault == "connection":
            raise requests.ConnectionError("synthetic")
        return cookie_response(status=fault, headers={"Retry-After": "0"})

    calls = install_cookie_transport(handler)
    with HttpSession() as session, pytest.raises(AuthError):
        _follow(session, url, "synthetic", via_vpn=True)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        SSO_BASE + "/cas/granting",
        VPN_IDENTITY_BASE + "/cas/granting",
        VPN_ORIGIN + "/http/webvpnabcdef?ticket=fake-ticket",
        VPN_API_BASE + VALIDATE_PATH + "?ticket=fake-ticket",
    ],
)
def test_gateway_progress_never_releases_consumed_tickets(install_cookie_transport, url):
    responses = iter([redirect(authorization()), redirect(VPN_PRELOGIN_URL), redirect(url)])
    calls = install_cookie_transport(lambda *args: next(responses))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, url, "synthetic", via_vpn=True)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "url,via_vpn",
    [
        (VPN_AUTHORIZATION, False),
        (VPN_AUTHORIZATION + "/", True),
        (VPN_AUTHORIZATION + "-other", True),
        (VPN_ORIGIN + "/engateway/api/other", True),
        (VPN_AUTHORIZATION.replace("https:", "http:"), True),
        (VPN_AUTHORIZATION.replace(":8443", ":8444"), True),
        (VPN_AUTHORIZATION.replace("vpn.njupt.edu.cn", "vpn.njupt.edu.cn.evil.test"), True),
        (VPN_AUTHORIZATION.replace("https://", "https://evil@"), True),
        (VPN_ORIGIN + "/http/webvpnabcdef-evil", True),
        (VPN_ORIGIN + "/http/webvpnabcdef/%2e%2e/other", True),
        (VPN_ORIGIN + "/http/webvpnabcdef/%252e%252e/other", True),
        ("http://i.njupt.edu.cn.evil.test/user-login", False),
        ("http://evil@i.njupt.edu.cn/user-login", False),
        ("http://i.njupt.edu.cn:8080/user-login", False),
        ("http://i.njupt.edu.cn/other", False),
        ("https://i.njupt.edu.cn/user-login/%2e%2e/other", False),
    ],
)
def test_new_routes_preserve_host_path_and_network_restrictions(
    install_cookie_transport, url, via_vpn
):
    calls = install_cookie_transport(lambda *args: pytest.fail("Unapproved request"))
    with HttpSession() as session, pytest.raises(AuthProtocolError):
        _follow(session, url, "synthetic", via_vpn=via_vpn)
    assert not calls


@pytest.mark.parametrize("via_vpn", [False, True])
@pytest.mark.parametrize("slash", ["", "/"])
def test_resource_landing_retains_ticket_and_nested_service(
    install_cookie_transport, via_vpn, slash
):
    root = VPN_ORIGIN + "/http/webvpnabcdef" if via_vpn else SERVICE_URL.rstrip("/")
    url = root + slash + "?ticket=fake-ticket&service=http%3A%2F%2Fexample.test%2F%3Fa%3Db%26c%3Dd"
    calls = install_cookie_transport(lambda *args: cookie_response(text="done"))
    with HttpSession() as session:
        _follow(session, url, "synthetic", via_vpn=via_vpn).close()
    assert len(calls) == 1 and calls[0][1].url == requests.Request("GET", url).prepare().url
    assert urlsplit(calls[0][1].url).query == urlsplit(url).query


def test_gateway_secret_is_registered_and_redacted_in_both_log_outputs(
    install_cookie_transport, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    secret = "synthetic+gateway/key"
    stream = io.StringIO()
    redactor = Redactor()
    calls = install_cookie_transport(lambda *args: cookie_response(text="done"))
    with configure_logging(True, redactor=redactor, stream=stream):
        with HttpSession(redactor=redactor) as session:
            _follow(session, authorization(secret), "synthetic", via_vpn=True).close()
        try:
            raise ValueError(secret + " " + quote(secret, safe=""))
        except ValueError:
            logging.getLogger("njupt_auth.test").debug("synthetic error", exc_info=True)
    output = stream.getvalue()
    assert len(calls) == 1 and "Traceback" in output
    assert secret not in output and quote(secret, safe="") not in output
    assert (tmp_path / "labpass_log.txt").read_text(encoding="utf-8") == output
