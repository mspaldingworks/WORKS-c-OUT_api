from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity import google_oauth
from identity.models import GoogleDriveConnection
from tracker.drive import resolve_drive

# The test block in settings blanks all Google creds; per-user credential
# building needs the OAuth client id/secret present, so resolve_drive tests
# override them.
OAUTH_CLIENT = {"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec"}


class DriveConnectionModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")

    def test_token_encrypted_round_trip(self):
        conn = GoogleDriveConnection(owner=self.user)
        conn.set_token("1//refresh-secret")
        conn.save()
        self.assertNotIn("refresh-secret", conn.refresh_token_encrypted)
        self.assertEqual(conn.get_token(), "1//refresh-secret")
        self.assertTrue(conn.connected)

    def test_not_connected_without_token(self):
        self.assertFalse(GoogleDriveConnection(owner=self.user).connected)


class ResolveDriveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")

    @override_settings(**OAUTH_CLIENT)
    def test_prefers_user_connection(self):
        conn = GoogleDriveConnection(owner=self.user, folder_id="folderX", enabled=True)
        conn.set_token("user-refresh")
        conn.save()
        creds, folder = resolve_drive(self.user)
        self.assertEqual(folder, "folderX")
        self.assertEqual(creds.refresh_token, "user-refresh")

    @override_settings(**OAUTH_CLIENT)
    def test_disabled_connection_is_not_used(self):
        conn = GoogleDriveConnection(owner=self.user, folder_id="folderX", enabled=False)
        conn.set_token("user-refresh")
        conn.save()
        # Not the default owner in a multi-user DB, and no server creds → None.
        get_user_model().objects.create_user("other", password="x")
        self.assertIsNone(resolve_drive(self.user))

    @override_settings(**OAUTH_CLIENT, GOOGLE_OAUTH_REFRESH_TOKEN="server-refresh",
                       JOB_DRIVE_FOLDER_ID="serverFolder", WORKS_COUT_OWNER_EMAIL="owner@x.test")
    def test_owner_falls_back_to_server_drive(self):
        owner = get_user_model().objects.create_user("owner", email="owner@x.test", password="x")
        creds, folder = resolve_drive(owner)
        self.assertEqual(folder, "serverFolder")
        self.assertEqual(creds.refresh_token, "server-refresh")

    @override_settings(**OAUTH_CLIENT, GOOGLE_OAUTH_REFRESH_TOKEN="server-refresh",
                       JOB_DRIVE_FOLDER_ID="serverFolder", WORKS_COUT_OWNER_EMAIL="owner@x.test")
    def test_other_user_without_connection_gets_nothing(self):
        get_user_model().objects.create_user("owner", email="owner@x.test", password="x")
        other = get_user_model().objects.create_user("other", email="other@x.test", password="x")
        self.assertIsNone(resolve_drive(other))


class DriveEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")
        self.token = Token.objects.create(user=self.user)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Token {self.token.key}"}

    def test_status_not_connected(self):
        resp = self.client.get(reverse("drive-connection"), **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["connected"])

    def test_status_and_toggle_when_connected(self):
        conn = GoogleDriveConnection(owner=self.user, folder_id="f", account_email="u@x.com", enabled=True)
        conn.set_token("rt")
        conn.save()
        body = self.client.get(reverse("drive-connection"), **self._auth()).json()
        self.assertTrue(body["connected"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["account_email"], "u@x.com")

        resp = self.client.patch(reverse("drive-connection"), data={"enabled": False},
                                 content_type="application/json", **self._auth())
        self.assertFalse(resp.json()["enabled"])
        conn.refresh_from_db()
        self.assertFalse(conn.enabled)

    def test_patch_requires_a_connection(self):
        resp = self.client.patch(reverse("drive-connection"), data={"enabled": False},
                                 content_type="application/json", **self._auth())
        self.assertEqual(resp.status_code, 400)

    @override_settings(**OAUTH_CLIENT)
    def test_connect_returns_auth_url(self):
        resp = self.client.get(reverse("drive-connect"), **self._auth())
        self.assertEqual(resp.status_code, 200)
        url = resp.json()["auth_url"]
        self.assertIn("accounts.google.com", url)
        self.assertIn("cid", url)
        self.assertIn("state=", url)

    def test_connect_503_when_unconfigured(self):
        resp = self.client.get(reverse("drive-connect"), **self._auth())
        self.assertEqual(resp.status_code, 503)

    @patch("tracker.drive.ensure_folder", return_value="folder123")
    @patch("tracker.drive.credentials_for", return_value=object())
    @patch("identity.google_oauth.fetch_email", return_value="u@x.com")
    @patch("identity.google_oauth.exchange_code",
           return_value={"refresh_token": "rt", "access_token": "at"})
    def test_callback_stores_connection_and_redirects(self, _exchange, _email, _creds, _folder):
        state = google_oauth.make_state(self.user.pk)
        resp = self.client.get(reverse("drive-callback") + f"?code=abc&state={state}")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("workscout://drive-connected"))
        self.assertIn("ok=1", resp["Location"])

        conn = GoogleDriveConnection.objects.get(owner=self.user)
        self.assertEqual(conn.get_token(), "rt")
        self.assertEqual(conn.folder_id, "folder123")
        self.assertEqual(conn.account_email, "u@x.com")
        self.assertTrue(conn.enabled)

    def test_callback_error_redirects_ok0(self):
        resp = self.client.get(reverse("drive-callback") + "?error=access_denied")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("ok=0", resp["Location"])

    def test_callback_bad_state_redirects_ok0(self):
        resp = self.client.get(reverse("drive-callback") + "?code=abc&state=garbage")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("ok=0", resp["Location"])

    def test_disconnect_deletes(self):
        conn = GoogleDriveConnection(owner=self.user)
        conn.set_token("rt")
        conn.save()
        resp = self.client.post(reverse("drive-disconnect"), **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["connected"])
        self.assertEqual(GoogleDriveConnection.objects.filter(owner=self.user).count(), 0)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(reverse("drive-connection")).status_code, 401)
