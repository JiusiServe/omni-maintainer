"""Service-account access to private dashboards, using ordinary browser sessions.

Keep this client standard-library only. The standalone deployment guard embeds
the same protocol because it is copied to another repository without this package.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
from pathlib import Path
import shlex
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable


class DashboardAuthError(ValueError):
    """A terminal authentication/configuration error containing no secrets."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def service_credentials(path: Path) -> dict[str, str]:
    """Read owner-only environment assignments as data, never as shell code."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 16384):
                raise ValueError
            values = {}
            for line in stream:
                parts = shlex.split(line, comments=True)
                if not parts:
                    continue
                if len(parts) != 1 or "=" not in parts[0]:
                    raise ValueError
                key, value = parts[0].split("=", 1)
                if key not in {"REVIEWBOT_DASHBOARD_USERNAME", "REVIEWBOT_DASHBOARD_PASSWORD"} or key in values:
                    raise ValueError
                values[key] = value
            if not all(values.get(key) for key in (
                    "REVIEWBOT_DASHBOARD_USERNAME", "REVIEWBOT_DASHBOARD_PASSWORD")):
                raise ValueError
            return values
    except (OSError, UnicodeError, ValueError):
        raise DashboardAuthError("dashboard service credential file is missing or invalid") from None


class DashboardClient:
    """One in-memory cookie jar shared by both configured dashboard paths."""

    def __init__(self, bases: Iterable[str], *, username: str | None = None,
                 password: str | None = None):
        self._bases = tuple(base.rstrip("/") for base in bases)
        for base in self._bases:
            try:
                parsed = urllib.parse.urlsplit(base)
                valid = (parsed.scheme == "https" and parsed.hostname
                         and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment
                         and (parsed.port is None or parsed.port > 0)
                         and "\\" not in base
                         and not any(character.isspace() or ord(character) < 32 for character in base))
            except ValueError:
                valid = False
            if not valid:
                raise DashboardAuthError("dashboard URLs must use HTTPS without credentials")
        self._username = username
        self._password = password
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies), _NoRedirect(),
        )

    @classmethod
    def from_credentials_file(cls, bases: Iterable[str], path: Path):
        values = service_credentials(path)
        return cls(bases, username=values["REVIEWBOT_DASHBOARD_USERNAME"],
                   password=values["REVIEWBOT_DASHBOARD_PASSWORD"])

    def _base(self, url: str) -> str:
        for base in sorted(self._bases, key=len, reverse=True):
            if url.startswith(base + "/api/"):
                parsed = urllib.parse.urlsplit(url)
                if not parsed.fragment:
                    return base
        raise DashboardAuthError("dashboard request is outside the configured API paths")

    def _request(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            # Never include server error bodies: they may echo credentials.
            status = exc.code
            exc.close()
            if 300 <= status < 400:
                raise DashboardAuthError("dashboard redirects are not permitted") from None
            return status, b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OSError(f"dashboard request failed ({type(exc).__name__})") from None

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        base = self._base(url)
        request = urllib.request.Request(url, headers={"User-Agent": "omni-maintainer/0.1"})
        status, body = self._request(request, timeout)
        if status != 401:
            return status, body
        username = self._username if self._username is not None else os.environ.get("REVIEWBOT_DASHBOARD_USERNAME", "")
        password = self._password if self._password is not None else os.environ.get("REVIEWBOT_DASHBOARD_PASSWORD", "")
        if not username or not password:
            raise DashboardAuthError("dashboard service credentials are not configured")
        parsed = urllib.parse.urlsplit(base)
        authority = parsed.netloc.lower()
        if parsed.port == 443:
            authority = authority.rsplit(":", 1)[0]
        login = urllib.request.Request(
            base + "/api/auth/login", method="POST",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "omni-maintainer/0.1",
                     "Origin": f"https://{authority}"},
        )
        try:
            status, _ = self._request(login, timeout)
        except OSError:
            raise DashboardAuthError("dashboard login request failed") from None
        if status != 200:
            raise DashboardAuthError(f"dashboard login failed (HTTP {status})")
        # A Request retains its Cookie header; rebuilding lets CookieJar attach
        # the replacement cookie after an expired session was renewed.
        request = urllib.request.Request(url, headers={"User-Agent": "omni-maintainer/0.1"})
        try:
            status, body = self._request(request, timeout)
        except OSError:
            raise DashboardAuthError("dashboard request failed after login") from None
        if status == 401:
            raise DashboardAuthError("dashboard session was rejected after login")
        return status, body
