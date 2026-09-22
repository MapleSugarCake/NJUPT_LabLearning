"""Strict offline flows with synthetic credentials and real CookieJar processing."""

import json
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import pytest
import requests
from conftest import cookie_response, success

from njupt_auth import NetworkEnvironment, authenticate, check_access
from njupt_auth.config import (
    CHECK_KEY,
    INTRANET_API_BASE,
    PERMISSION_PATH,
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
from njupt_auth.crypto import encrypt
from njupt_auth.errors import AuthError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import ResourceSessionFactory

USER = "synthetic-user"
PASSWORD = " synthetic password "
IDENTITY_TOKEN = "fake-identity-token"
LAB_TOKEN = "fake-lab-token"
GUEST = "GUESTSESSIONID=fake-guest"
ENS = "ENSSESSIONID=fake-ens"
GATEWAY = "gateway_context=fake-context"
VPN_COOKIES = (GUEST, ENS, GATEWAY)


def query(url, **params):
    return url + ("&" if "?" in url else "?") + urlencode(params)


def redirect(location, *cookies, status=302):
    return cookie_response(status=status, headers={"Location": location}, cookies=cookies)


@dataclass(slots=True)
class Step:
    url: str
    response: requests.Response | BaseException
    method: str = "GET"
    cookies: tuple[str, ...] | None = None
    app_id: str | None = None


def identity_entry(base, service, service_id, cookie):
    login = query(base + "/user-login", service=service_id)
    location = login.replace(SSO_BASE, "http://i.njupt.edu.cn")
    return [
        Step(query(base + "/cas/login", service=service), redirect(location, cookie)),
        Step(login, redirect(query(base + "/user-login/", service=service_id), status=301)),
        Step(query(base + "/user-login/", service=service_id), cookie_response(text="login")),
    ]


def identity_post(base, service_id, app_id, *, fallback=False):
    suffix = "?enlink-vpn" if base == VPN_IDENTITY_BASE else ""
    config = {"appId": app_id if fallback else "fake-unselected-app", "loginAppId": ""}
    if not fallback:
        config["loginAppId"] = app_id
    cookie = (
        "vpn_timestamp=fake-time-post; Path=/; Secure"
        if base == VPN_IDENTITY_BASE
        else f"tgc={IDENTITY_TOKEN}; Path=/; HttpOnly"
    )
    return [
        Step(
            query(
                base + "/AppLoginAllocation/queryLoginAllocationByService" + suffix,
                service=service_id,
            ),
            cookie_response(success(config)),
        ),
        Step(
            base + "/ssoLogin/login" + suffix,
            cookie_response(success({"token": IDENTITY_TOKEN}), cookies=(cookie,)),
            method="POST",
            app_id=app_id,
        ),
    ]


def permission_step(base, *, via_vpn):
    url = base + PERMISSION_PATH
    cookies = ("lab_session=fake-lab-session",)
    if via_vpn:
        url = query(url, _t="fake-time-final")
        cookies = (*VPN_COOKIES, "vpn_timestamp=fake-time-final", *cookies)
    return Step(url, cookie_response(success({"menu": []}, code=0)), cookies=cookies)


def resource_steps(base, *, via_vpn):
    params = {"ticket": "fake-lab-ticket", "service": SERVICE_URL}
    cookies = ()
    if via_vpn:
        params["_t"] = "fake-time-final"
        cookies = (*VPN_COOKIES, "vpn_timestamp=fake-time-final")
    return [
        Step(
            query(base + VALIDATE_PATH, **params),
            cookie_response(
                success({"token": LAB_TOKEN}),
                cookies=("lab_session=fake-lab-session; Path=/; HttpOnly",),
            ),
            cookies=cookies,
        ),
        permission_step(base, via_vpn=via_vpn),
    ]


def campus_flow(*, fallback=False):
    service_id = "fake-campus-service"
    landing = query(SERVICE_URL.rstrip("/"), ticket="fake-lab-ticket", service=SERVICE_URL)
    steps = identity_entry(
        SSO_BASE, SERVICE_URL, service_id, "JSESSIONID=fake-campus-session; Path=/"
    )
    steps += identity_post(SSO_BASE, service_id, "fake-campus-app", fallback=fallback)
    steps += [
        Step(
            query(SSO_BASE + "/ssoLogin/index", sessionId=service_id),
            redirect(query("http://i.njupt.edu.cn/cas/login", service=SERVICE_URL)),
            cookies=("JSESSIONID=fake-campus-session", "tgc=" + IDENTITY_TOKEN),
        ),
        Step(
            query(SSO_BASE + "/cas/login", service=SERVICE_URL),
            redirect("http://i.njupt.edu.cn/cas/granting"),
        ),
        Step(SSO_BASE + "/cas/granting", redirect(landing)),
        Step(landing, cookie_response(text="lab landing")),
    ]
    return steps + resource_steps(INTRANET_API_BASE, via_vpn=False)


def vpn_flow(*, fallback=False):
    gateway = query(
        VPN_ORIGIN + "/enlink/sso/login", redirectUrl=VPN_PRELOGIN_URL, gate="fake-gate"
    )
    authorized = query(
        VPN_AUTHORIZATION, redirect_url=VPN_PRELOGIN_URL, entoken="fake+authorization/key"
    )
    callback = query(VPN_CALLBACK, ticket="fake-vpn-ticket", service=VPN_CALLBACK)
    steps = [
        Step(VPN_PRELOGIN_URL, redirect(VPN_PRELOGIN_URL, GUEST + "; Path=/; Secure"), cookies=()),
        Step(VPN_PRELOGIN_URL, redirect(gateway), cookies=(GUEST,)),
        Step(
            gateway,
            cookie_response(text="gateway", cookies=(ENS + "; Path=/; Secure; HttpOnly",)),
            cookies=(GUEST,),
        ),
    ]
    steps += identity_entry(
        SSO_BASE, VPN_CALLBACK, "fake-vpn-service", "JSESSIONID=fake-vpn-session; Path=/"
    )
    steps += identity_post(SSO_BASE, "fake-vpn-service", "fake-vpn-app", fallback=fallback)
    steps += [
        Step(
            query(SSO_BASE + "/ssoLogin/index", sessionId="fake-vpn-session"),
            redirect(query("http://i.njupt.edu.cn/cas/login", service=VPN_CALLBACK)),
            cookies=("JSESSIONID=fake-vpn-session", "tgc=" + IDENTITY_TOKEN),
        ),
        Step(
            query(SSO_BASE + "/cas/login", service=VPN_CALLBACK),
            redirect("http://i.njupt.edu.cn/cas/granting"),
        ),
        Step(SSO_BASE + "/cas/granting", redirect(callback)),
        Step(
            callback,
            redirect(VPN_PRELOGIN_URL, GATEWAY + "; Path=/; Secure; HttpOnly"),
            cookies=(GUEST, ENS),
        ),
        Step(VPN_PRELOGIN_URL, redirect(gateway), cookies=VPN_COOKIES),
        Step(gateway, redirect(authorized), cookies=VPN_COOKIES),
        # Authorization changes server-side state, without a Set-Cookie response.
        Step(authorized, redirect(VPN_PRELOGIN_URL), cookies=VPN_COOKIES),
        Step(VPN_PRELOGIN_URL, cookie_response(text="authenticated gateway"), cookies=VPN_COOKIES),
    ]
    steps += identity_entry(
        VPN_IDENTITY_BASE,
        SERVICE_URL,
        "fake-mapped-service",
        "vpn_timestamp=fake-time-entry; Path=/; Secure",
    )
    steps += identity_post(
        VPN_IDENTITY_BASE, "fake-mapped-service", "fake-mapped-app", fallback=fallback
    )
    landing = query(
        VPN_ORIGIN + "/http/webvpnabcdef", ticket="fake-lab-ticket", service=SERVICE_URL
    )
    steps += [
        Step(
            query(VPN_IDENTITY_BASE + "/ssoLogin/index", sessionId="fake-mapped-service"),
            redirect(query(VPN_IDENTITY_BASE + "/cas/login", service=SERVICE_URL)),
            cookies=(*VPN_COOKIES, "vpn_timestamp=fake-time-post"),
        ),
        Step(
            query(VPN_IDENTITY_BASE + "/cas/login", service=SERVICE_URL),
            redirect(
                VPN_IDENTITY_BASE + "/cas/granting",
                "vpn_timestamp=fake-time-cas; Path=/; Secure",
            ),
        ),
        Step(
            VPN_IDENTITY_BASE + "/cas/granting",
            redirect(landing, "vpn_timestamp=fake-time-final; Path=/; Secure"),
            cookies=(*VPN_COOKIES, "vpn_timestamp=fake-time-cas"),
        ),
        Step(landing, cookie_response(text="mapped lab landing")),
    ]
    return steps + resource_steps(VPN_API_BASE, via_vpn=True)


def install_flow(install_cookie_transport, steps):
    remaining = iter(steps)
    responses = []

    def handler(request, kwargs):
        step = next(remaining, None)
        assert step is not None, "Unexpected request after the authentication flow"
        assert request.url == requests.Request(step.method, step.url).prepare().url
        assert request.method == step.method
        assert kwargs["timeout"] == (10.0, 30.0)
        cookies = request.headers.get("Cookie", "")
        if step.cookies is not None:
            assert set(cookies.split("; ") if cookies else ()) == set(step.cookies)
        if urlsplit(request.url).path.endswith(PERMISSION_PATH):
            assert request.headers["X-Access-Token"] == LAB_TOKEN
        else:
            assert "X-Access-Token" not in request.headers
        if not request.url.startswith(SSO_BASE):
            assert IDENTITY_TOKEN not in cookies
        if step.app_id:
            assert json.loads(request.body) == {
                "checkKey": CHECK_KEY,
                "username": encrypt(USER),
                "password": encrypt(PASSWORD),
                "captchaVerification": None,
                "appId": step.app_id,
                "mode": "none",
            }
        if isinstance(step.response, BaseException):
            raise step.response
        responses.append(step.response)
        return step.response

    return install_cookie_transport(handler), responses


@pytest.mark.parametrize("environment", list(NetworkEnvironment))
@pytest.mark.parametrize("fallback", [False, True])
def test_strict_password_flows_probe_and_hand_over_independent_sessions(
    install_cookie_transport, environment, fallback
):
    via_vpn = environment is NetworkEnvironment.EXTRANET
    base = VPN_API_BASE if via_vpn else INTRANET_API_BASE
    steps = (vpn_flow if via_vpn else campus_flow)(fallback=fallback)
    steps.append(permission_step(base, via_vpn=via_vpn))
    calls, responses = install_flow(install_cookie_transport, steps)
    redactor = Redactor()
    with authenticate(USER, PASSWORD, environment=environment, redactor=redactor) as result:
        assert result.api_base_url == base
        assert calls[0][0].closed
        assert isinstance(result.session, requests.Session) and not result.session.closed
        with result.session_factory() as clone:
            check_access(clone, base, redactor=redactor)
            assert clone is not result.session and clone.cookies is not result.session.cookies
            for main_cookie, clone_cookie in zip(
                result.session.cookies, clone.cookies, strict=True
            ):
                assert main_cookie is not clone_cookie
                assert vars(main_cookie) == vars(clone_cookie)
            for scheme in result.session.adapters:
                assert result.session.adapters[scheme] is not clone.adapters[scheme]
                assert clone.adapters[scheme].max_retries.total == 0
            clone.cookies.clear()
            assert result.session.cookies
    assert len(calls) == len(steps)
    assert all(session.closed for session, _, _ in calls)
    assert all(response.close.called for response in responses)
    assert all(not request.url.startswith("http://i.njupt.edu.cn") for _, request, _ in calls)
    with pytest.raises(RuntimeError):
        result.session_factory()


@pytest.mark.parametrize(
    "environment,index",
    [
        (environment, index)
        for environment, flow in (
            (NetworkEnvironment.INTRANET, campus_flow),
            (NetworkEnvironment.EXTRANET, vpn_flow),
        )
        for index in range(len(flow()))
    ],
)
@pytest.mark.parametrize("interrupt", [False, True])
def test_each_flow_failure_or_interrupt_releases_resources(
    install_cookie_transport, monkeypatch, environment, index, interrupt
):
    steps = (vpn_flow if environment is NetworkEnvironment.EXTRANET else campus_flow)()[: index + 1]
    steps[-1].response = KeyboardInterrupt() if interrupt else cookie_response(status=403)
    factories = []

    def factory(*args, **kwargs):
        result = ResourceSessionFactory(*args, **kwargs)
        factories.append(result)
        return result

    monkeypatch.setattr("njupt_auth.auth.ResourceSessionFactory", factory)
    calls, responses = install_flow(install_cookie_transport, steps)
    with pytest.raises(KeyboardInterrupt if interrupt else AuthError):
        authenticate(USER, PASSWORD, environment=environment)
    assert len(calls) == len(steps)
    assert all(session.closed for session, _, _ in calls)
    assert all(response.close.called for response in responses)
    for created in factories:
        with pytest.raises(RuntimeError):
            created()
