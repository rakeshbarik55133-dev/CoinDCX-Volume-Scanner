"""One-time FYERS API v3 access-token generator.

This helper is intentionally separate from the CoinDCX scanner and does not
place orders or automate trading. It only performs the FYERS API v3 user-app
authentication flow so a human can create a short-lived access token and save it
as the FYERS_ACCESS_TOKEN GitHub Secret.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

FYERS_AUTH_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
FYERS_TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
REQUEST_TIMEOUT = 30
RESPONSE_TYPE = "code"
GRANT_TYPE = "authorization_code"
STATE = "fyers-token-generator"


@dataclass(frozen=True)
class FyersCredentials:
    app_id: str
    secret_key: str
    redirect_uri: str


def read_credentials() -> FyersCredentials:
    """Read required FYERS credentials from environment variables."""
    app_id = os.getenv("FYERS_APP_ID", "").strip()
    secret_key = os.getenv("FYERS_SECRET_KEY", "").strip()
    redirect_uri = os.getenv("FYERS_REDIRECT_URI", "").strip()

    missing = [
        name
        for name, value in (
            ("FYERS_APP_ID", app_id),
            ("FYERS_SECRET_KEY", secret_key),
            ("FYERS_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    return FyersCredentials(app_id=app_id, secret_key=secret_key, redirect_uri=redirect_uri)


def build_login_url(credentials: FyersCredentials) -> str:
    """Build the FYERS authorization URL without exposing the app secret."""
    query = urlencode(
        {
            "client_id": credentials.app_id,
            "redirect_uri": credentials.redirect_uri,
            "response_type": RESPONSE_TYPE,
            "state": STATE,
        }
    )
    return f"{FYERS_AUTH_URL}?{query}"


def extract_auth_code(redirected_url: str) -> str:
    """Extract auth_code/code from the pasted redirect URL."""
    parsed = urlparse(redirected_url.strip())
    query_values = parse_qs(parsed.query)
    fragment_values = parse_qs(parsed.fragment)
    values = query_values | fragment_values

    auth_code = (values.get("auth_code") or values.get("code") or [""])[0].strip()
    if not auth_code:
        raise ValueError("Could not find auth_code or code in the redirected URL")
    return auth_code


def app_id_hash(credentials: FyersCredentials) -> str:
    """Return FYERS v3 SHA-256 appIdHash for app_id:secret_key."""
    return hashlib.sha256(
        f"{credentials.app_id}:{credentials.secret_key}".encode("utf-8")
    ).hexdigest()


def exchange_auth_code(credentials: FyersCredentials, auth_code: str) -> str:
    """Exchange the auth code for an access token using FYERS API v3."""
    payload = {
        "grant_type": GRANT_TYPE,
        "appIdHash": app_id_hash(credentials),
        "code": auth_code,
    }
    response = requests.post(FYERS_TOKEN_URL, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data: dict[str, Any] = response.json()

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        message = str(data.get("message") or data.get("s") or "unknown FYERS response")
        raise RuntimeError(f"FYERS did not return an access token: {message}")
    return access_token


def main() -> int:
    try:
        credentials = read_credentials()
        print(build_login_url(credentials), flush=True)
        redirected_url = input("Paste the full redirected URL here: ").strip()
        auth_code = extract_auth_code(redirected_url)
        access_token = exchange_auth_code(credentials, auth_code)
        print(access_token)
        return 0
    except (RuntimeError, ValueError, requests.RequestException) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
