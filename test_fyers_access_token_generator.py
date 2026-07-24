import os
import sys
import types
import unittest
from unittest.mock import Mock, patch
import fyers_access_token_generator as generator


class FyersAccessTokenGeneratorTests(unittest.TestCase):
    def test_read_credentials_requires_all_environment_variables(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FYERS_APP_ID"):
                generator.read_credentials()

    def test_build_login_url_uses_official_fyers_v3_session(self) -> None:
        credentials = generator.FyersCredentials(
            app_id="APP-100",
            secret_key="never-printed",
            redirect_uri="https://example.com/callback",
        )
        session = Mock()
        session.generate_authcode.return_value = (
            "https://api-t1.fyers.in/api/v3/generate-authcode?client_id=APP-100"
        )

        with patch.object(generator, "build_session", return_value=session) as build_session:
            url = generator.build_login_url(credentials)

        self.assertEqual(
            url,
            "https://api-t1.fyers.in/api/v3/generate-authcode?client_id=APP-100",
        )
        build_session.assert_called_once_with(credentials)
        session.generate_authcode.assert_called_once_with()
        self.assertNotIn("never-printed", url)

    def test_build_session_passes_required_fyers_v3_parameters(self) -> None:
        credentials = generator.FyersCredentials(
            app_id="APP-100",
            secret_key="secret",
            redirect_uri="https://example.com/callback",
        )
        session_model = Mock(return_value="session")
        fake_fyers_apiv3 = types.SimpleNamespace(
            fyersModel=types.SimpleNamespace(SessionModel=session_model)
        )

        with patch.dict(sys.modules, {"fyers_apiv3": fake_fyers_apiv3}):
            session = generator.build_session(credentials)

        self.assertEqual(session, "session")
        session_model.assert_called_once_with(
            client_id="APP-100",
            secret_key="secret",
            redirect_uri="https://example.com/callback",
            response_type="code",
            grant_type="authorization_code",
            state="fyers-token-generator",
        )

    def test_extract_auth_code_supports_auth_code_or_code_parameters(self) -> None:
        self.assertEqual(
            generator.extract_auth_code("https://example.com/callback?auth_code=abc123&state=x"),
            "abc123",
        )
        self.assertEqual(
            generator.extract_auth_code("https://example.com/callback?code=xyz789"),
            "xyz789",
        )

    @patch("fyers_access_token_generator.build_session")
    def test_exchange_auth_code_uses_sdk_token_flow(self, build_session: Mock) -> None:
        session = Mock()
        session.generate_token.return_value = {"access_token": "APP-100:token"}
        build_session.return_value = session
        credentials = generator.FyersCredentials("APP-100", "secret", "https://example.com")

        token = generator.exchange_auth_code(credentials, "auth-code-value")

        self.assertEqual(token, "APP-100:token")
        build_session.assert_called_once_with(credentials)
        session.set_token.assert_called_once_with("auth-code-value")
        session.generate_token.assert_called_once_with()

    @patch("fyers_access_token_generator.exchange_auth_code", return_value="APP-100:token")
    def test_main_with_redirected_url_writes_new_token_file(self, exchange: Mock) -> None:
        env = {
            "FYERS_APP_ID": "APP-100",
            "FYERS_SECRET_KEY": "secret",
            "FYERS_REDIRECT_URI": "https://example.com/callback",
            "FYERS_REDIRECTED_URL": "https://example.com/callback?auth_code=abc123",
        }
        with patch.dict(os.environ, env, clear=True), patch("builtins.print") as printed:
            with patch("fyers_access_token_generator.TOKEN_OUTPUT_FILE", "test_new_token.txt"):
                try:
                    exit_code = generator.main()
                    with open("test_new_token.txt", encoding="utf-8") as token_file:
                        self.assertEqual(token_file.read(), "APP-100:token")
                finally:
                    if os.path.exists("test_new_token.txt"):
                        os.remove("test_new_token.txt")

        self.assertEqual(exit_code, 0)
        exchange.assert_called_once()
        self.assertEqual(exchange.call_args.args[1], "abc123")
        printed.assert_called_once_with("Access token saved to test_new_token.txt")


if __name__ == "__main__":
    unittest.main()
