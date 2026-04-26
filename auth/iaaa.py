"""
PKU IAAA SSO authentication.

Actual flow (reverse-engineered from OAuthLogin.js on iaaa.pku.edu.cn):
  1. GET /iaaa/getPublicKey.do → RSA public key (PEM)
  2. Encrypt password with RSA PKCS1v15 using that key
  3. POST /iaaa/oauthlogin.do with appid, userName, encrypted password, redirectUrl
     → returns JSON { success: true, token: "..." }
  4. GET campusLogin?token=... on course.pku.edu.cn → Blackboard session cookies

PKU CA is not in the default macOS trust store, so verify=False is used.

NOTE:
  - IAAA and Blackboard use separate session cookie namespaces.
  - IAAA's JSESSIONID MUST NOT be sent to Blackboard; otherwise Blackboard
    ignores per-student query parameters (userId/filePk/attemptPk) and returns
    stale/default content.
  - We use the `requests` library (not httpx) because it has a simpler cookie
    model that does not raise CookieConflict when multiple Set-Cookie headers
    with the same name arrive during a redirect chain.
"""
from __future__ import annotations

import base64
import json
from typing import Optional

import requests

from config import settings

IAAA_BASE = "https://iaaa.pku.edu.cn"
# IAAA registered redirect URL (must stay http:// — the server rejects https:// here).
IAAA_REDIRECT_URL = (
    "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
)
# Actual campusLogin endpoint (PKU now requires HTTPS).
BB_CAMPUS_LOGIN_URL = (
    "https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
)
BB_BASE = "https://course.pku.edu.cn"


def _encrypt_password(password: str, public_key_pem: str) -> str:
    """RSA-encrypt password using PKCS1v15 (matching JSEncrypt behaviour)."""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(key, RSAPublicKey)
    encrypted = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def get_session() -> requests.Session:
    """
    Authenticate via IAAA and return a requests.Session with an active
    Blackboard session.

    Cookie handling: requests does not raise CookieConflict when multiple
    Set-Cookie headers with the same name arrive. We maintain a simple
    ``_bb_cookies`` dict and inject it as a raw ``Cookie:`` header on every
    request, bypassing the session's own cookie jar entirely for BB cookies.
    This guarantees only the confirmed-good s_session_id is ever sent to Blackboard.
    """
    if not settings.pku_username or not settings.pku_password:
        raise RuntimeError(
            "PKU_USERNAME and PKU_PASSWORD must be set in .env to authenticate."
        )

    # Blackboard cookies collected during campusLogin redirect walk
    _bb_cookies: dict[str, str] = {}

    def _auth_headers() -> dict[str, str]:
        if not _bb_cookies:
            return {}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in _bb_cookies.items())}

    def _follow(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
        """Follow redirects manually, collecting BB cookies from every response."""
        resp = session.request(method, url, **kwargs)
        for _ in range(20):
            # Collect Set-Cookie headers on every response
            for header in resp.raw.headers.getlist("set-cookie"):
                name_val, _, _ = header.partition(";")
                name, _, val = name_val.partition("=")
                name, val = name.strip(), val.strip()
                if name and name != "JSESSIONID":
                    _bb_cookies[name] = val
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = resp.headers.get("location", "")
            if not location:
                break
            if not location.startswith(("http://", "https://")):
                location = str(session.base_url).rstrip("/") + "/" + location.lstrip("/")
            resp = session.request("GET", location, **kwargs)
        return resp

    # Step 1-3: IAAA SSO (separate session)
    iaaa_session = requests.Session()
    iaaa_session.verify = False  # PKU CA not in default trust store
    iaaa_session.timeout = 30

    key_resp = iaaa_session.get(
        f"{IAAA_BASE}/iaaa/getPublicKey.do",
        headers={"Accept": "application/json"},
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()
    if not key_data.get("success"):
        raise RuntimeError(f"Failed to fetch IAAA public key: {key_data}")
    public_key_pem: str = key_data["key"]

    encrypted_pwd = _encrypt_password(settings.pku_password, public_key_pem)

    login_resp = iaaa_session.post(
        f"{IAAA_BASE}/iaaa/oauthlogin.do",
        data={
            "appid": "blackboard",
            "userName": settings.pku_username,
            "password": encrypted_pwd,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": IAAA_REDIRECT_URL,
        },
    )
    login_resp.raise_for_status()
    payload = login_resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"IAAA login failed: {payload.get('errors') or payload}")
    token: str = payload["token"]

    # Step 4: Blackboard campusLogin (separate session)
    bb_session = requests.Session()
    bb_session.verify = False
    bb_session.timeout = 30
    bb_session.headers.update({"Accept": "text/html,application/xhtml+xml,*/*"})

    resp = _follow(bb_session, "GET", BB_CAMPUS_LOGIN_URL, params={"token": token})
    resp.raise_for_status()

    # Step 5: Build final session with only confirmed BB cookies
    final_session = requests.Session()
    final_session.verify = False
    final_session.timeout = 30

    # Inject cookies via a CookieJar-like approach: store in session and send
    # as explicit header on every request, bypassing the jar's auto-management.
    _session_cookie_jar = {}

    def _inject_cookies(method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("headers", {})
        cookie_header = "; ".join(f"{k}={v}" for k, v in _session_cookie_jar.items())
        if cookie_header:
            kwargs["headers"]["Cookie"] = cookie_header
        return final_session.request(method, url, **kwargs)

    # Store the confirmed cookies
    _session_cookie_jar.update(_bb_cookies)

    if not _session_cookie_jar.get("s_session_id"):
        raise RuntimeError(
            "Expected Blackboard session cookie 's_session_id' not found after SSO. "
            f"Cookies collected: {list(_bb_cookies.keys())}"
        )

    # Return a lightweight wrapper that always sends the confirmed cookies
    # and delegates to the requests session.
    class _BBClient:
        """Thin wrapper around requests.Session that injects BB cookies on every call."""

        base_url = BB_BASE

        def get(self, url: str, **kwargs) -> requests.Response:
            if not url.startswith(("http://", "https://")):
                url = self.base_url.rstrip("/") + "/" + url.lstrip("/")
            return _inject_cookies("GET", url, **kwargs)

        def post(self, url: str, **kwargs) -> requests.Response:
            if not url.startswith(("http://", "https://")):
                url = self.base_url.rstrip("/") + "/" + url.lstrip("/")
            return _inject_cookies("POST", url, **kwargs)

        def request(self, method: str, url: str, **kwargs) -> requests.Response:
            return _inject_cookies(method, url, **kwargs)

    return _BBClient()
