import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import urlencode

import pytest
from conftest import make_response, success

from njupt_auth import NetworkEnvironment, authenticate_in_browser
from njupt_auth.auth import api_base
from njupt_auth.browser import _cookie_jar, matches_validation_response
from njupt_auth.config import SERVICE_URL, VALIDATE_PATH, VPN_CALLBACK
from njupt_auth.errors import AuthError, BrowserUnavailableError


def browser_response(environment, *, service=SERVICE_URL, token="fake-browser-token"):
    return SimpleNamespace(
        url=api_base(environment)
        + VALIDATE_PATH
        + "?"
        + urlencode({"service": service, "ticket": "fake-browser-ticket"}),
        status=200,
        request=SimpleNamespace(method="GET"),
        json=lambda: success({"token": token}),
    )


@pytest.fixture
def fake_browser(monkeypatch):
    def install(environment, *, capture=True, cancelled=False, failure=False, vpn_redirect=False):
        state = SimpleNamespace(
            closed=False,
            goto=[],
            cookies_urls=[],
            options={},
            response=browser_response(environment),
        )

        class BrowserError(Exception):
            pass

        class Page:
            def goto(self, url, **kwargs):
                state.goto.append(url)
                if vpn_redirect and len(state.goto) == 1:
                    state.observe(
                        SimpleNamespace(
                            url=VPN_CALLBACK, status=302, request=SimpleNamespace(method="GET")
                        )
                    )
                elif capture:
                    state.observe(state.response)

            def is_closed(self):
                return cancelled

            def evaluate(self, expression):
                assert expression == "navigator.userAgent"
                return "synthetic-browser-agent"

            def wait_for_timeout(self, duration):
                pass

        class Context:
            def new_page(self):
                return Page()

            def on(self, event, callback):
                assert event == "response"
                state.observe = callback

            def cookies(self, urls):
                state.cookies_urls.extend(urls)
                domain = (
                    "vpn.njupt.edu.cn"
                    if environment is NetworkEnvironment.EXTRANET
                    else "10.22.192.38"
                )
                return [
                    {
                        "name": "gateway",
                        "value": "fake-gateway-cookie",
                        "domain": domain,
                        "path": "/",
                        "secure": environment is NetworkEnvironment.EXTRANET,
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": -1,
                    }
                ]

        class Browser:
            def new_context(self, **kwargs):
                state.options = kwargs
                return Context()

            def is_connected(self):
                return True

            def close(self):
                state.closed = True

        class Chromium:
            def launch(self, **kwargs):
                assert kwargs == {"channel": "msedge", "headless": False}
                if failure:
                    raise BrowserError("synthetic error with ticket=secret")
                return Browser()

        class Manager:
            def __enter__(self):
                return SimpleNamespace(chromium=Chromium())

            def __exit__(self, *args):
                pass

        module = ModuleType("playwright.sync_api")
        module.Error = BrowserError
        module.sync_playwright = Manager
        monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
        return state

    return install


@pytest.mark.parametrize("environment", list(NetworkEnvironment))
def test_browser_response_to_verified_independent_session(
    fake_browser, install_transport, environment
):
    state = fake_browser(environment, vpn_redirect=environment is NetworkEnvironment.EXTRANET)
    calls = install_transport(lambda *args: make_response(success({"menu": []}, code=0)))
    with authenticate_in_browser(environment=environment) as result:
        assert state.closed
        assert calls[0][1].headers["X-Access-Token"] == "fake-browser-token"
        assert "gateway=fake-gateway-cookie" in calls[0][1].headers["Cookie"]
        assert calls[0][1].headers["User-Agent"] == "synthetic-browser-agent"
        assert state.options == {"accept_downloads": False}
        assert state.cookies_urls == [api_base(environment) + VALIDATE_PATH]
        with result.session_factory() as worker:
            assert worker.cookies is not result.session.cookies
    assert result.session.closed
    assert len(state.goto) == (2 if environment is NetworkEnvironment.EXTRANET else 1)


@pytest.mark.parametrize(
    "change", ["origin", "port", "path", "service", "method", "status", "ticket"]
)
def test_capture_rejects_other_sources(change):
    environment = NetworkEnvironment.EXTRANET
    response = browser_response(environment)
    if change == "origin":
        response.url = response.url.replace("vpn.njupt.edu.cn", "evil.test")
    elif change == "port":
        response.url = response.url.replace(":8443", ":443")
    elif change == "path":
        response.url = response.url.replace("/jeecg-boot/sys/", "/portal/")
    elif change == "service":
        response = browser_response(environment, service="https://i.njupt.edu.cn/portal/")
    elif change == "method":
        response.request.method = "POST"
    elif change == "status":
        response.status = 403
    else:
        response.url = response.url.replace("ticket=fake-browser-ticket", "ticket=")
    assert not matches_validation_response(response, environment)


def test_missing_optional_dependency_is_recoverable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(BrowserUnavailableError, match="browser"):
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)


def test_missing_edge_is_recoverable_and_does_not_expose_driver_error(fake_browser):
    fake_browser(NetworkEnvironment.INTRANET, failure=True)
    with pytest.raises(BrowserUnavailableError) as error:
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)
    assert "secret" not in str(error.value)


def test_cancelled_browser_is_closed(fake_browser):
    state = fake_browser(NetworkEnvironment.INTRANET, cancelled=True)
    with pytest.raises(AuthError, match="取消"):
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)
    assert state.closed


def test_browser_timeout_is_bounded_and_closes_context(fake_browser, monkeypatch):
    state = fake_browser(NetworkEnvironment.INTRANET, capture=False)
    clock = iter([0, 301])
    monkeypatch.setattr("njupt_auth.browser.time.monotonic", lambda: next(clock))
    with pytest.raises(AuthError, match="5 分钟"):
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)
    assert state.closed


def test_browser_probe_failure_closes_browser_and_session(fake_browser, install_transport):
    state = fake_browser(NetworkEnvironment.EXTRANET)
    calls = install_transport(lambda *args: make_response(status=401))
    with pytest.raises(AuthError):
        authenticate_in_browser(environment=NetworkEnvironment.EXTRANET)
    assert state.closed and calls[0][0].closed


def test_cookie_conversion_preserves_security_attributes():
    jar = _cookie_jar(
        [
            {
                "name": "fake",
                "value": "fake-value",
                "domain": ".example.test",
                "path": "/resource",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Strict",
                "expires": 1999999999,
            }
        ]
    )
    cookie = next(iter(jar))
    assert cookie.domain == ".example.test" and cookie.path == "/resource"
    assert cookie.secure and cookie.expires == 1999999999
    assert cookie.get_nonstandard_attr("HttpOnly") is True
    assert cookie.get_nonstandard_attr("SameSite") == "Strict"
