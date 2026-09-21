import pytest
import requests
from conftest import make_response

from njupt_auth.config import INTRANET_API_BASE, REQUEST_TIMEOUT, VPN_API_BASE
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession, ResourceSessionFactory, cookie_value


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_get_retries_exact_statuses_at_most_three(install_transport, status):
    calls = install_transport(lambda *args: make_response(status=status))
    sleeps = []
    with HttpSession(sleep=sleeps.append) as session:
        assert session.get("https://example.test/").status_code == status
        assert all(adapter.max_retries.total == 0 for adapter in session.adapters.values())
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    assert all(
        call[2]["timeout"] == REQUEST_TIMEOUT and call[2]["allow_redirects"] is False
        for call in calls
    )


@pytest.mark.parametrize("status", [200, 301, 401, 403, 404, 413, 501])
def test_other_statuses_never_retry_even_with_retry_after(install_transport, status):
    calls = install_transport(
        lambda *args: make_response(status=status, headers={"Retry-After": "0"})
    )
    with HttpSession(sleep=lambda _: None) as session:
        session.get("https://example.test/")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "method,error,count",
    [
        ("GET", requests.ConnectTimeout, 3),
        ("GET", requests.ReadTimeout, 3),
        ("GET", requests.ConnectionError, 3),
        ("GET", requests.exceptions.SSLError, 1),
        ("POST", requests.ConnectTimeout, 1),
        ("POST", requests.ReadTimeout, 1),
        ("POST", requests.ConnectionError, 1),
    ],
)
def test_network_retry_boundaries(install_transport, method, error, count):
    def fail(*args):
        raise error("synthetic failure")

    calls = install_transport(fail)
    with HttpSession(sleep=lambda _: None) as session, pytest.raises(error):
        session.request(method, "https://example.test/")
    assert len(calls) == count


@pytest.mark.parametrize("status", [307, 308, 429, 500, 503])
def test_post_never_retries_or_follows_redirects(install_transport, status):
    calls = install_transport(
        lambda *args: make_response(status=status, headers={"Location": "https://other.test/"})
    )
    with HttpSession(sleep=lambda _: None) as session:
        session.post("https://example.test/", allow_redirects=True, json={"fake": "payload"})
    assert len(calls) == 1


def test_single_use_get_does_not_retry(install_transport):
    calls = install_transport(lambda *args: make_response(status=503))
    with HttpSession() as session:
        session.get("https://example.test/validateLogin", retry_get=False)
    assert len(calls) == 1


def test_session_factory_independence_cookie_attributes_and_scope(install_transport):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("vpn_timestamp", "fake-timestamp", domain="vpn.njupt.edu.cn", path="/", secure=True)
    jar.set(
        "gateway",
        "fake-cookie",
        domain="vpn.njupt.edu.cn",
        path="/",
        secure=True,
        rest={"HttpOnly": True},
    )
    jar.set("tgc", "fake-identity-token", domain="i.njupt.edu.cn", path="/")
    redactor = Redactor()
    factory = ResourceSessionFactory(
        VPN_API_BASE, "fake-resource-token", jar, via_vpn=True, redactor=redactor
    )
    calls = install_transport(lambda *args: make_response({}))
    with factory() as first, factory() as second:
        assert isinstance(first, requests.Session)
        assert first is not second and first.cookies is not second.cookies
        assert first.get_adapter("https://") is not second.get_adapter("https://")
        first_cookie, second_cookie = list(first.cookies)[0], list(second.cookies)[0]
        assert first_cookie is not second_cookie
        assert first_cookie.secure and first_cookie.domain == "vpn.njupt.edu.cn"
        first_cookie.value = "changed"
        assert second_cookie.value == "fake-timestamp"
        second.get(VPN_API_BASE + "/resource", params={"id": "course-x"})
        second.post(VPN_API_BASE + "/write", json={"id": "course-x"})
        assert "_t=fake-timestamp" in calls[0][1].url
        assert calls[1][1].url.endswith("?enlink-vpn")
        assert calls[0][1].headers["X-Access-Token"] == "fake-resource-token"
        assert "tgc" not in calls[0][1].headers.get("Cookie", "")
        assert "X-Access-Token" not in second.headers
        for url in (
            "https://evil.test/",
            VPN_API_BASE + "/../portal",
            VPN_API_BASE + "/%2e%2e/portal",
        ):
            with pytest.raises(requests.exceptions.InvalidURL):
                second.get(url)
    assert first.closed and second.closed
    factory.close()
    with pytest.raises(RuntimeError):
        factory()


def test_cookie_resolution_obeys_destination_and_path():
    jar = requests.cookies.RequestsCookieJar()
    jar.set("JSESSIONID", "fake-other", domain="other.test", path="/")
    jar.set("JSESSIONID", "fake-root", domain="example.test", path="/")
    jar.set("JSESSIONID", "fake-specific", domain="example.test", path="/ssoLogin")
    assert cookie_value(jar, "JSESSIONID", "https://example.test/ssoLogin/index") == "fake-specific"


def test_direct_resource_does_not_add_vpn_params(install_transport):
    factory = ResourceSessionFactory(
        INTRANET_API_BASE,
        "fake-token",
        requests.cookies.RequestsCookieJar(),
        via_vpn=False,
        redactor=Redactor(),
    )
    calls = install_transport(lambda *args: make_response({}))
    with factory() as session:
        session.get(INTRANET_API_BASE + "/x")
        session.post(INTRANET_API_BASE + "/x")
    assert all("?" not in call[1].url for call in calls)
    factory.close()
