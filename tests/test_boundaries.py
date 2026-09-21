import ast
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests
from conftest import make_response, success
from test_auth import auth_server

from labpass_cli.runner import CourseRunner
from njupt_auth import NetworkEnvironment, authenticate
from njupt_auth.config import SSO_BASE
from njupt_auth.errors import AuthOutcomeUncertainError, InteractionRequiredError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession, ResourceSessionFactory
from njupt_safetylabpass import Course, MutationCoordinator, SafetyLabClient
from njupt_safetylabpass.exceptions import RunCancelledError


@pytest.mark.parametrize("status", [307, 308])
def test_auth_post_redirect_is_not_followed_or_retried(install_transport, status):
    def handler(session, request, kwargs):
        if request.method == "POST":
            return make_response(status=status, headers={"Location": SSO_BASE + "/ssoLogin/login"})
        return auth_server(session, request, kwargs)

    calls = install_transport(handler)
    with pytest.raises(AuthOutcomeUncertainError):
        authenticate(
            "synthetic-user", "synthetic-password", environment=NetworkEnvironment.INTRANET
        )
    assert sum(request.method == "POST" for _, request, _ in calls) == 1


def test_application_id_fallback_and_challenge_classification(install_transport):
    def handler(session, request, kwargs):
        if urlsplit(request.url).path.endswith("queryLoginAllocationByService"):
            return make_response(success({"appId": "fake-lab-service-app"}))
        if request.method == "POST":
            return make_response({"success": False, "code": 400, "message": "需要验证码"})
        return auth_server(session, request, kwargs)

    calls = install_transport(handler)
    with pytest.raises(InteractionRequiredError):
        authenticate(
            "synthetic-user", "synthetic-password", environment=NetworkEnvironment.INTRANET
        )
    assert calls[-1][1].method == "POST" and calls[0][0].closed


def test_none_timeout_still_has_connection_and_read_limits(install_transport):
    calls = install_transport(lambda *args: make_response({}))
    with HttpSession() as session:
        session.get("https://example.test/", timeout=None)
    assert calls[0][2]["timeout"] == (10.0, 30.0)


def test_keyboard_interrupt_cancels_scheduler_and_closes_sessions(install_transport):
    calls = install_transport(
        lambda session, request, kwargs: make_response(
            success([] if request.method == "GET" else None)
        )
    )
    base = "https://example.test/api"
    factory = ResourceSessionFactory(
        base, "fake-token", requests.cookies.RequestsCookieJar(), via_vpn=False, redactor=Redactor()
    )
    coordinator = MutationCoordinator()
    with SafetyLabClient(
        factory(), api_base_url=base, session_factory=factory, mutation_coordinator=coordinator
    ) as client:
        completed = Mock(side_effect=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            CourseRunner(client, 1, completed=completed).run(
                [Course(str(i), "synthetic") for i in range(20)]
            )
        with pytest.raises(RunCancelledError):
            coordinator.check_cancelled()
    assert all(session.closed for session, _, _ in calls)
    factory.close()


def test_architecture_has_no_reverse_imports_or_legacy_parser():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "labpass").exists()
    for package in ("njupt_auth", "njupt_safetylabpass", "labpass_cli"):
        for path in (root / package).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports.append(node.module.split(".")[0])
            assert "argparse" not in imports and "labpass" not in imports
            if package != "labpass_cli":
                assert "labpass_cli" not in imports
            if package == "njupt_auth":
                assert "njupt_safetylabpass" not in imports
