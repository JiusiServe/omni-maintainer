"""Exercise real urllib cookie handling without sending credentials to a network."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import urllib.error
import urllib.request
import urllib.response
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

from omni_maintainer import cli
from omni_maintainer.monitor.dashboard import Fetch, fetch_json
from omni_maintainer.monitor.dashboard_auth import DashboardAuthError, DashboardClient, _NoRedirect, service_credentials


ORIGIN = "https://review.example"
BASES = (ORIGIN + "/code_review/vllm_omni", ORIGIN + "/code_review/vllm_gr")
PASSWORD = "private-test-password"


def _guard():
    path = Path(__file__).resolve().parents[1] / "workflows/rb-canary-guard.py"
    spec = importlib.util.spec_from_file_location("authenticated_canary_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=("monitor", "standalone_guard"))
def client_class(request):
    return DashboardClient if request.param == "monitor" else _guard().DashboardClient


class DashboardHTTPS(urllib.request.BaseHandler):
    """A transport fake; urllib's cookie and HTTP error processors remain real."""

    handler_order = 100

    def __init__(self):
        self.requests = []
        self.session = "first-session"
        self.protected = True
        self.login_status = 200
        self.issue_cookie = True
        self.redirect_get = False
        self.get_status = None

    def https_open(self, request):
        self.requests.append(request)
        headers = Message()
        if request.get_method() == "POST":
            assert request.full_url.endswith("/api/auth/login")
            assert request.get_header("Origin") == ORIGIN
            assert request.get_header("Content-type") == "application/json"
            assert json.loads(request.data) == {"username": "monitor", "password": PASSWORD}
            status = self.login_status
            if status == 200 and self.issue_cookie:
                headers["Set-Cookie"] = f"session={self.session}; Path=/code_review/; Secure; HttpOnly; SameSite=Lax"
            if 300 <= status < 400:
                headers["Location"] = "https://untrusted.example/receive"
            body = PASSWORD.encode()  # error responses must never leak this
        elif self.redirect_get:
            status, body = 302, b""
            headers["Location"] = "https://untrusted.example/receive"
        elif self.get_status is not None:
            status, body = self.get_status, PASSWORD.encode()
        elif not self.protected or request.get_header("Cookie") == f"session={self.session}":
            status, body = 200, json.dumps({"jobs": [], "history": {}, "id": 7}).encode()
        else:
            status, body = 401, PASSWORD.encode()
        response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, status)
        response.msg = "test response"
        return response


def _client(client_class):
    client = client_class(BASES, username="monitor", password=PASSWORD)
    server = DashboardHTTPS()
    client._opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(client._cookies), _NoRedirect(), server,
    )
    return client, server


def test_session_is_shared_across_instances_and_renewed_after_expiry(client_class):
    client, server = _client(client_class)

    assert client(BASES[0] + "/api/status", 5)[0] == 200
    assert client(BASES[1] + "/api/jobs/7", 5)[0] == 200
    assert [r.get_method() for r in server.requests] == ["GET", "POST", "GET", "GET"]
    assert server.requests[-1].get_header("Cookie") == "session=first-session"
    server.session = "renewed-session"
    assert client(BASES[1] + "/api/status", 5)[0] == 200
    assert [r.get_method() for r in server.requests[-3:]] == ["GET", "POST", "GET"]
    assert server.requests[-2].full_url == BASES[1] + "/api/auth/login"
    assert not hasattr(client._cookies, "filename")


def test_older_public_app_needs_no_login_during_cutover(client_class, monkeypatch):
    monkeypatch.delenv("REVIEWBOT_DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("REVIEWBOT_DASHBOARD_PASSWORD", raising=False)
    client, server = _client(client_class)
    client._username = client._password = None
    server.protected = False

    assert client(BASES[0] + "/api/status", 5)[0] == 200
    assert len(server.requests) == 1


@pytest.mark.parametrize("login_status", (401, 403, 404, 500))
def test_login_errors_never_retry_anonymously_or_echo_password(client_class, login_status):
    client, server = _client(client_class)
    server.login_status = login_status

    with pytest.raises(ValueError, match="dashboard login failed") as error:
        client(BASES[0] + "/api/status", 5)

    assert PASSWORD not in str(error.value)
    assert [r.get_method() for r in server.requests] == ["GET", "POST"]


def test_successful_login_without_a_session_retries_exactly_once(client_class):
    client, server = _client(client_class)
    server.issue_cookie = False

    with pytest.raises(ValueError, match="session was rejected"):
        client(BASES[0] + "/api/status", 5)

    assert [r.get_method() for r in server.requests] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("stage", ("get", "login"))
def test_redirects_are_not_followed(client_class, stage):
    client, server = _client(client_class)
    server.redirect_get = stage == "get"
    server.login_status = 302

    with pytest.raises(ValueError, match="redirects are not permitted"):
        client(BASES[0] + "/api/status", 5)

    assert all(r.full_url.startswith(ORIGIN + "/") for r in server.requests)
    assert len(server.requests) == (1 if stage == "get" else 2)


@pytest.mark.parametrize("base", ("http://review.example/code_review/a", "https://u:secret@review.example/a",
                                  "https://@review.example/a", "https://review.example/a?token=secret",
                                  "https://[invalid/a", "https://review.example:99999/a",
                                  "https://review.example:0/a", "https://review.example\n/a",
                                  "https://review.example\\evil/a"))
def test_unsafe_configuration_is_rejected_before_any_request(client_class, base):
    with pytest.raises(ValueError, match="HTTPS without credentials") as error:
        client_class((base,))
    assert "secret" not in str(error.value)


def test_unconfigured_origin_and_paths_are_rejected(client_class):
    client, server = _client(client_class)
    for url in ("https://untrusted.example/api/status", ORIGIN + "/api/status", BASES[0] + "/api/status#fragment"):
        with pytest.raises(ValueError, match="outside the configured API paths"):
            client(url, 5)
    assert server.requests == []


def test_non401_denial_does_not_submit_credentials(client_class):
    client, server = _client(client_class)
    server.get_status = 403
    assert client(BASES[0] + "/api/status", 5) == (403, b"")
    assert len(server.requests) == 1


def test_environment_credentials_and_missing_credentials(monkeypatch):
    client, server = _client(DashboardClient)
    client._username = client._password = None
    monkeypatch.delenv("REVIEWBOT_DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("REVIEWBOT_DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        client(BASES[0] + "/api/status", 5)
    monkeypatch.setenv("REVIEWBOT_DASHBOARD_USERNAME", "monitor")
    monkeypatch.setenv("REVIEWBOT_DASHBOARD_PASSWORD", PASSWORD)
    assert client(BASES[0] + "/api/status", 5)[0] == 200


def test_authentication_failure_stops_fetch_retries_without_secret_output():
    client, server = _client(DashboardClient)
    server.login_status = 401
    sleeps = []
    result = fetch_json(BASES[0] + "/api/status", timeout=5, attempts=3, retry_seconds=1,
                        opener=client, sleep=sleeps.append)
    assert not result.ok and result.attempts == 1
    assert PASSWORD not in result.error
    assert len(server.requests) == 2 and sleeps == []


def test_login_transport_failure_does_not_submit_credentials_again():
    client, server = _client(DashboardClient)
    original_request = client._request

    def broken_login(request, timeout):
        if request.get_method() == "POST":
            raise OSError(PASSWORD)
        return original_request(request, timeout)

    client._request = broken_login
    sleeps = []
    result = fetch_json(BASES[0] + "/api/status", timeout=5, attempts=3, retry_seconds=1,
                        opener=client, sleep=sleeps.append)
    assert not result.ok and result.attempts == 1
    assert result.error == "dashboard login request failed"
    assert len(server.requests) == 1 and sleeps == []


def test_injected_transport_errors_are_sanitized_and_retried():
    def broken(url, timeout):
        raise urllib.error.URLError(PASSWORD)

    sleeps = []
    result = fetch_json(BASES[0] + "/api/status", timeout=5, attempts=3, retry_seconds=1,
                        opener=broken, sleep=sleeps.append)
    assert not result.ok and result.attempts == 3
    assert PASSWORD not in result.error and sleeps == [1, 1]


def test_cli_status_and_job_read_share_authenticated_helper(monkeypatch, capsys):
    calls = []

    def read(url, **kwargs):
        calls.append((url, kwargs))
        return Fetch(True, 0.1, {"id": 7})

    monkeypatch.setattr(cli, "fetch_json", read)
    assert cli.main(["monitor", "read", "--instance", "vllm_omni"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": 7}
    assert cli.main(["monitor", "read", "--instance", "vllm_gr", "--job", "7"]) == 0
    assert calls[0][0].startswith("https://") and calls[0][0].endswith("/api/status")
    assert calls[1][0].endswith("/api/jobs/7")
    assert all(isinstance(kwargs["opener"], DashboardClient) for _, kwargs in calls)
    assert cli.main(["monitor", "read", "--instance", "unknown"]) == cli.EXIT_USAGE
    assert cli.main(["monitor", "read", "--instance", "vllm_gr", "--job", "0"]) == cli.EXIT_USAGE
    assert len(calls) == 2


def test_monitor_fetches_both_instances_using_one_cookie_client(monkeypatch, policy):
    clients = []

    def read(url, **kwargs):
        clients.append(kwargs["opener"])
        return Fetch(True, 0.1, {"jobs": [], "history": {}})

    monkeypatch.setattr(cli, "fetch_json", read)
    digests = cli._fetch_digests(policy, datetime.now(timezone.utc))
    assert len(digests) == 2 and clients[0] is clients[1]


def test_standalone_guard_shares_one_client_and_stops_on_auth_failure(monkeypatch, capsys):
    guard = _guard()
    clients = []

    class Denied:
        def __call__(self, url, timeout):
            clients.append(self)
            raise guard.DashboardAuthError("dashboard login failed (HTTP 401)")

    monkeypatch.setattr(guard, "DashboardClient", lambda bases: Denied())
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: pytest.fail("retried authentication failure"))
    with pytest.raises(SystemExit) as error:
        guard.read_dashboards()
    assert error.value.code == 1 and len(clients) == 1
    assert PASSWORD not in capsys.readouterr().err


def _credentials_file(tmp_path, password=PASSWORD):
    path = tmp_path / "service.env"
    path.write_text("REVIEWBOT_DASHBOARD_USERNAME=monitor\n"
                    + "REVIEWBOT_DASHBOARD_PASSWORD=" + repr(password) + "\n")
    path.chmod(0o600)
    return path


def test_credential_file_is_data_not_executable_shell(tmp_path):
    sentinel = tmp_path / "executed"
    password = f"$(touch {sentinel})"
    path = _credentials_file(tmp_path, password)
    assert service_credentials(path)["REVIEWBOT_DASHBOARD_PASSWORD"] == password
    assert not sentinel.exists()
    client = DashboardClient.from_credentials_file(BASES, path)
    assert client._password == password and password not in repr(client)


@pytest.mark.parametrize("kind", ("missing", "public", "symlink", "fifo", "duplicate", "extra", "large", "owner"))
def test_unsafe_credential_files_fail_without_secret_output(tmp_path, monkeypatch, kind):
    path = _credentials_file(tmp_path)
    if kind == "missing":
        path.unlink()
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "linked.env"
        link.symlink_to(path)
        path = link
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "duplicate":
        path.write_text(path.read_text() + "REVIEWBOT_DASHBOARD_USERNAME=duplicate\n")
    elif kind == "extra":
        path.write_text(path.read_text() + "UNEXPECTED=secret\n")
    elif kind == "large":
        path.write_text(path.read_text() + "#" * 16385)
    elif kind == "owner":
        current = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: current + 1)
    with pytest.raises(DashboardAuthError) as error:
        service_credentials(path)
    assert PASSWORD not in str(error.value)


def test_cli_reads_protected_credentials_without_exposing_or_sourcing_them(tmp_path, monkeypatch, capsys):
    path = _credentials_file(tmp_path)
    clients = []

    def read(url, **kwargs):
        clients.append(kwargs["opener"])
        return Fetch(True, 0.1, {"jobs": []})

    monkeypatch.setattr(cli, "fetch_json", read)
    assert cli.main(["monitor", "read", "--instance", "vllm_omni", "--credentials-file", str(path)]) == 0
    assert clients[0]._username == "monitor" and clients[0]._password == PASSWORD
    assert PASSWORD not in capsys.readouterr().out

    monkeypatch.setattr(cli, "_read_cursors", lambda *args: ({}, ""))

    def digests(policy, now, *, client):
        assert client._username == "monitor" and client._password == PASSWORD
        raise DashboardAuthError("authentication unavailable")

    monkeypatch.setattr(cli, "_fetch_digests", digests)
    assert cli.main(["monitor", "tick", "--credentials-file", str(path)]) == cli.EXIT_CRASH
    assert PASSWORD not in capsys.readouterr().out


def test_default_https_port_uses_browser_canonical_origin():
    base = "https://review.example:443/code_review/vllm_omni"
    client = DashboardClient((base,), username="monitor", password=PASSWORD)
    server = DashboardHTTPS()
    client._opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(client._cookies), _NoRedirect(), server,
    )
    assert client(base + "/api/status", 5)[0] == 200
