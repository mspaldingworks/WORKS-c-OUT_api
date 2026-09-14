"""
Google OAuth web-flow for connecting a user's own Drive.

Server-side flow: the app opens `auth_url(user)`, the user consents, Google
redirects to our callback with a code, and `exchange_code` swaps it for a refresh
token (stored encrypted on identity.GoogleDriveConnection). The user identity is
carried through the signed `state` param, since the browser callback has no auth
header. Token exchange uses stdlib urllib — same pattern as ingestion/mappers.py,
no extra dependency.
"""

import json
import urllib.parse
import urllib.request

from django.conf import settings
from django.core import signing

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

# drive.file: only files this app creates (privacy-preserving). openid+email so we
# can show "Connected as …" — both are non-sensitive and don't add to verification.
SCOPE = "openid email https://www.googleapis.com/auth/drive.file"

_STATE_SALT = "identity.drive.oauth.state"
_STATE_MAX_AGE = 600  # 10 minutes to complete the consent


class OAuthError(Exception):
    """OAuth couldn't complete; message is safe to surface to the user."""


def _client_id():
    # The dedicated Web client for the per-user flow; falls back to the server
    # client only if the dedicated one isn't set.
    return settings.GOOGLE_DRIVE_OAUTH_CLIENT_ID or settings.GOOGLE_OAUTH_CLIENT_ID


def _client_secret():
    return settings.GOOGLE_DRIVE_OAUTH_CLIENT_SECRET or settings.GOOGLE_OAUTH_CLIENT_SECRET


def client_configured():
    return bool(_client_id() and _client_secret())


def make_state(user_id):
    return signing.dumps({"uid": user_id}, salt=_STATE_SALT)


def read_state(state):
    try:
        data = signing.loads(state or "", salt=_STATE_SALT, max_age=_STATE_MAX_AGE)
    except signing.SignatureExpired as error:
        raise OAuthError("This sign-in link expired — start again from the app.") from error
    except signing.BadSignature as error:
        raise OAuthError("Invalid sign-in state.") from error
    return data["uid"]


def auth_url(user_id):
    params = {
        "client_id": _client_id(),
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent so Google always returns a refresh token, even on a
        # re-connect where it would otherwise omit it.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": make_state(user_id),
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(code):
    """Swap an authorization code for tokens. Returns the token payload dict."""
    body = urllib.parse.urlencode({
        "code": code or "",
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    request = urllib.request.Request(
        TOKEN_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        raise OAuthError(f"Google token exchange failed: {error}") from error
    if not payload.get("refresh_token"):
        # Happens when the account previously authorized without prompt=consent;
        # revoking the app's access and reconnecting fixes it.
        raise OAuthError("Google didn't return a refresh token — remove the app's access at "
                         "myaccount.google.com and reconnect.")
    return payload


def fetch_email(access_token):
    """Best-effort connected-account email for display. Never raises."""
    if not access_token:
        return ""
    request = urllib.request.Request(
        USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https host
            return json.loads(response.read().decode("utf-8")).get("email", "")
    except Exception:
        return ""
