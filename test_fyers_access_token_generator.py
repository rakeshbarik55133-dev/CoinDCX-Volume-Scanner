import hashlib
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import fyers_access_token_generator as generator


class FyersAccessTokenGeneratorTests(unittest.TestCase):
    def test_read_credentials_requires_all_environment_variables(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FYERS_APP_ID"):
                generator.read_credentials()

    def test_build_login_url_contains_required_fyers_v3_parameters(self) -> None:
        credentials = generator.FyersCredentials(
            app_id="APP-100",
            secret_key="never-printed",
            redirect_uri="https://example.com/callback",
        )

        url = generator.build_login_url(credentials)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(url.split("?")[0], generator.FYERS_AUTH_URL)
        self.assertEqual(query["client_id"], ["APP-100"])
        self.assertEqual(query["redirect_uri"], ["https://example.com/callback"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertNotIn("secret", url.lower())
        self.assertNotIn("never-printed", url)

    def test_extract_auth_code_supports_auth_code_or_code_parameters(self) -> None:
        self.assertEqual(
            generator.extract_auth_code("https://example.com/callback?auth_code=abc123&state=x"),
            "abc123",
        )
        self.assertEqual(
            generator.extract_auth_code("https://example.com/callback?code=xyz789"),
            "xyz789",
        )

    def test_app_id_hash_uses_fyers_app_id_colon_secret_format(self) -> None:
        credentials = generator.FyersCredentials("APP-100", "secret", "https://example.com")
        expected = hashlib.sha256(b"APP-100:secret").hexdigest()
        self.assertEqual(generator.app_id_hash(credentials), expected)

    @patch("fyers_access_token_generator.requests.post")
    def test_exchange_auth_code_posts_hash_and_returns_token_once(self, post: Mock) -> None:
        response = Mock()
        response.json.return_value = {"access_token": "APP-100:token"}
        response.raise_for_status.return_value = None
        post.return_value = response
        credentials = generator.FyersCredentials("APP-100", "secret", "https://example.com")

        token = generator.exchange_auth_code(credentials, "auth-code-value")

        self.assertEqual(token, "APP-100:token")
        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["grant_type"], "authorization_code")
        self.assertEqual(payload["code"], "auth-code-value")
        self.assertNotIn("secret", str(payload))


if __name__ == "__main__":
    unittest.main()
