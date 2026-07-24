"""One-time FYERS API v3 access-token generator using the official SDK flow.

This helper is intentionally separate from the CoinDCX scanner and does not
place orders or automate trading. It only performs the FYERS API v3 user-app
authentication flow so a human can create a short-lived access token and save it
as the FYERS_ACCESS_TOKEN GitHub Secret.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

TOKEN_OUTPUT_FILE = "new_token.txt"
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

    return FyersCredentials(
        app_id=app_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
    )


def build_session(credentials: FyersCredentials) -> Any:
    """Create the official FYERS API v3 SessionModel instance."""
    from fyers_apiv3 import fyersModel

    return fyersModel.SessionModel(
        client_id=credentials.app_id,
        secret_key=credentials.secret_key,
        redirect_uri=credentials.redirect_uri,
        response_type=RESPONSE_TYPE,
        grant_type=GRANT_TYPE,
        state=STATE,
    )


def build_login_url(credentials: FyersCredentials) -> str:
    """Build the FYERS authorization URL with the official SDK."""
    return str(build_session(credentials).generate_authcode())


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


def exchange_auth_code(credentials: FyersCredentials, auth_code: str) -> str:
    """Exchange the auth code for an access token using the official SDK."""
    session = build_session(credentials)
    session.set_token(auth_code)
    response = session.generate_token()

    if not isinstance(response, dict):
        raise RuntimeError(f"FYERS returned an unexpected token response: {response!r}")

    access_token = str(response.get("access_token") or "").strip()
    if not access_token:
        message = str(response.get("message") or response.get("s") or response)
        raise RuntimeError(f"FYERS did not return an access token: {message}")
    return access_token


def read_redirected_url() -> str:
    """Read the FYERS redirected URL from the environment when provided."""
    return os.getenv("FYERS_REDIRECTED_URL", "").strip()


def write_token(access_token: str, output_file: str | None = None) -> None:
    """Persist the generated access token to disk for GitHub Secret setup."""
    output_file = output_file or TOKEN_OUTPUT_FILE
    with open(output_file, "w", encoding="utf-8") as token_file:
        token_file.write(access_token)


def main() -> int:
    try:
        credentials = read_credentials()
        redirected_url = read_redirected_url()
        if not redirected_url:
            print(build_login_url(credentials), flush=True)
            return 0

        auth_code = extract_auth_code(redirected_url)
        access_token = exchange_auth_code(credentials, auth_code)
        write_token(access_token)
        print(f"Access token saved to {TOKEN_OUTPUT_FILE}")
        return 0
    except (RuntimeError, ValueError, ImportError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
