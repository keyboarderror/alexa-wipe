"""Authenticate to alexa.amazon.<region> via AlexaProxy and return a requests.Session.

Amazon killed the Alexa web portal and serves a passkey-first OAuth page when
scripts try to log in directly. The working approach in 2026 is a local HTTP
proxy that forwards to Amazon's real signin: the user logs in normally in their
browser (passkey, password, 2FA, CAPTCHA — all work because the browser is
real), the proxy captures the OAuth authorization code from the redirect, and
alexapy exchanges that code for long-lived Alexa-API cookies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi
import requests
import yaml

# Python.org Python on macOS ships without system CA trust. Before importing
# alexapy (which pulls in authcaptureproxy and its module-level
# `create_default_context()`), force both env vars and OpenSSL's default verify
# paths at certifi's bundle.
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

import ssl as _ssl  # noqa: E402

_orig_create_default_context = _ssl.create_default_context


def _ssl_create_default_context_with_certifi(*args: Any, **kwargs: Any) -> _ssl.SSLContext:
    ctx = _orig_create_default_context(*args, **kwargs)
    ctx.load_verify_locations(cafile=certifi.where())
    return ctx


_ssl.create_default_context = _ssl_create_default_context_with_certifi  # type: ignore[assignment]

from alexapy import AlexaLogin, AlexaProxy  # noqa: E402

_ALEXA_APP_UA = "com.amazon.dee.app/2.2.556644.0 (Linux; U; Android 11)"


@dataclass
class Config:
    email: str
    password: str
    totp_secret: str | None
    region: str

    @property
    def alexa_url(self) -> str:
        return f"https://alexa.amazon.{self.region}"


@dataclass
class ClientContext:
    session: requests.Session
    base_url: str
    csrf: str
    email: str


def load_config(path: str | Path) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    missing = [k for k in ("email", "password", "region") if not raw.get(k)]
    if missing:
        raise SystemExit(f"setup.yaml missing required fields: {', '.join(missing)}")
    if raw["region"] not in {"de", "com", "co.uk"}:
        raise SystemExit(f"unsupported region: {raw['region']} (use de, com, co.uk)")
    totp = raw.get("totp_secret") or None
    if isinstance(totp, str) and not totp.strip():
        totp = None
    return Config(
        email=str(raw["email"]).strip(),
        password=str(raw["password"]),
        totp_secret=totp,
        region=str(raw["region"]).strip(),
    )


def login(config: Config) -> ClientContext:
    login_obj = asyncio.run(_proxy_login(config))
    session = _session_from_alexapy(login_obj)
    csrf = (
        getattr(login_obj, "_csrf", None)
        or _cookie_for_domain(session, "csrf", f".amazon.{config.region}")
        or _prime_csrf(session, config.alexa_url)
    )
    session.headers.update(
        {
            "csrf": csrf,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": _ALEXA_APP_UA,
            "Referer": f"{config.alexa_url}/spa/index.html",
            "Origin": config.alexa_url,
        }
    )
    return ClientContext(session=session, base_url=config.alexa_url, csrf=csrf, email=config.email)


async def _proxy_login(config: Config) -> AlexaLogin:
    # Passing port 0 lets authcaptureproxy's get_open_port() bind a fresh free
    # port internally — avoids the pre-pick/re-bind TOCTOU race.
    base_url = "http://127.0.0.1:0"
    login_obj = AlexaLogin(
        url=f"amazon.{config.region}",
        email=config.email,
        password=config.password,
        outputpath=lambda _: os.devnull,
        debug=False,
        otp_secret=(config.totp_secret or ""),
    )
    proxy = AlexaProxy(login_obj, base_url)
    await proxy.start_proxy()
    access = str(proxy.access_url())
    await _self_test_proxy(access)
    print()
    print("=" * 72)
    print("  OPEN THIS URL IN YOUR BROWSER TO LOG IN:")
    print(f"  {access}")
    print("=" * 72)
    print()
    print("Sign in as you normally would. Passkey may fail because the browser sees")
    print("the origin as 127.0.0.1 — click 'Mit Passwort anmelden' / 'Use password")
    print("instead' and Amazon will show the password form (autofilled from your")
    print("setup.yaml). 2FA and CAPTCHA work normally. Ctrl-C aborts.")
    print()
    with contextlib.suppress(Exception):
        webbrowser.open(access)
    try:
        while not login_obj.authorization_code:
            await asyncio.sleep(1)
    finally:
        await proxy.stop_proxy(delay=1)
    print("Login captured. Exchanging OAuth code for Alexa API cookies …")
    ok = await login_obj.test_loggedin()
    if not ok:
        raise SystemExit(
            "Token exchange finished but the session isn't authenticated to the "
            "Alexa API. Rerun and try again."
        )
    return login_obj


def _cookie_for_domain(session: requests.Session, name: str, domain: str) -> str | None:
    """Pick a cookie by name from a specific domain. The jar may contain the same
    name for .amazon.com and .amazon.de after an OAuth round-trip, so bare
    session.cookies.get(name) raises CookieConflictError."""
    for cookie in session.cookies:
        if cookie.name == name and (cookie.domain == domain or cookie.domain == domain.lstrip(".")):
            return cookie.value
    return None


async def _self_test_proxy(access_url: str) -> None:
    """Confirm the proxy is actually serving before we show the URL to the user."""

    def _probe() -> tuple[int, str]:
        try:
            with urllib.request.urlopen(access_url, timeout=5) as resp:
                return resp.status, ""
        except Exception as exc:
            return 0, repr(exc)

    loop = asyncio.get_running_loop()
    status, err = await loop.run_in_executor(None, _probe)
    if status == 0:
        raise SystemExit(
            f"Proxy did not respond at {access_url} ({err}). "
            "Check for firewalls/VPNs that block localhost, then rerun."
        )


def _session_from_alexapy(login_obj: AlexaLogin) -> requests.Session:
    session = requests.Session()
    aiohttp_session = getattr(login_obj, "_session", None)
    if aiohttp_session is None or aiohttp_session.cookie_jar is None:
        raise SystemExit("alexapy did not expose an aiohttp session after login")
    for cookie in aiohttp_session.cookie_jar:
        session.cookies.set(
            cookie.key,
            cookie.value,
            domain=cookie["domain"] or "",
            path=cookie["path"] or "/",
        )
    return session


def _prime_csrf(session: requests.Session, alexa_url: str) -> str:
    resp = session.get(
        f"{alexa_url}/api/language",
        headers={"User-Agent": _ALEXA_APP_UA, "Referer": f"{alexa_url}/spa/index.html"},
        timeout=15,
    )
    resp.raise_for_status()
    # Derive the host domain from e.g. "https://alexa.amazon.de" -> ".amazon.de"
    domain = "." + alexa_url.split("://", 1)[1].split("/", 1)[0].split(".", 1)[1]
    csrf = _cookie_for_domain(session, "csrf", domain)
    if not csrf:
        raise SystemExit(
            "no csrf cookie was set after /api/language — token exchange likely incomplete"
        )
    return csrf
