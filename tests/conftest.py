"""Shared fixtures: sign-in settings and a stand-in for Google sign-in."""

from collections.abc import Callable

import httpx
import pytest

from buildserver import config

ADMIN_EMAIL = "admin@example.com"
ALLOWED_DOMAIN = "team.example.com"


@pytest.fixture
def auth_settings() -> config.AuthSettings:
    """Provides sign-in settings with one configured admin and one domain."""
    return config.AuthSettings(
        public_url="http://testserver",
        google_client_id="client-id",
        google_client_secret="client-secret",
        session_secret="test-session-secret",
        admin_emails=frozenset({ADMIN_EMAIL}),
        allowed_domains=frozenset({ALLOWED_DOMAIN}),
    )


@pytest.fixture
def sign_in() -> Callable[..., httpx.Response]:
    """Provides a function that signs a test client in as a Google account.

    The function fakes Google's answer to the token request and completes the
    callback, so everything after Google's redirect runs for real.
    """

    def _sign_in(client, email: str, verified: bool = True) -> httpx.Response:
        async def fake_token(request):
            del request  # Unused.
            return {
                "userinfo": {
                    "email": email,
                    "email_verified": verified,
                    "sub": f"google-{email}",
                    "name": email.partition("@")[0].title(),
                }
            }

        client.app.state.oauth.google.authorize_access_token = fake_token
        return client.get("/auth/callback", follow_redirects=False)

    return _sign_in
