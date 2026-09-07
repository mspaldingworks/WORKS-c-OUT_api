"""
Provisioning accounts for a partner app's members.

The properties that matter: the key provisions and nothing else, one member's
account can never reach another's rows, disconnecting really deletes, and
adding member accounts doesn't break the machine callers that resolve an owner
without a request — `get_default_owner` used to work because exactly one user
existed, which stops being true the moment a partner account is created.
"""

import io
import zipfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ResumeVersion, Skill
from identity.owners import get_default_owner
from identity.partner_views import partner_username

KEY = "partner-key-for-tests"


def docx_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


@override_settings(PARTNER_API_KEY=KEY)
class PartnerAccountTests(TestCase):
    def setUp(self):
        self.url = reverse("partner-accounts")

    def provision(self, external_id="member-1", email="member@example.test", key=KEY):
        return self.client.post(
            self.url,
            {"partner": "transwell", "external_id": external_id, "email": email},
            content_type="application/json",
            HTTP_X_PARTNER_KEY=key,
        )

    def test_creates_an_account_and_returns_its_token(self):
        response = self.provision()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["username"], "partner-transwell-member-1")
        user = get_user_model().objects.get(username=body["username"])
        self.assertEqual(Token.objects.get(user=user).key, body["token"])

    def test_the_account_cannot_be_signed_into_with_a_password(self):
        self.provision()

        user = get_user_model().objects.get(username="partner-transwell-member-1")
        self.assertFalse(user.has_usable_password())

    def test_provisioning_twice_returns_the_same_account_and_token(self):
        first = self.provision().json()
        second = self.provision()

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["token"], first["token"])
        self.assertEqual(get_user_model().objects.filter(
            username="partner-transwell-member-1").count(), 1)

    def test_a_wrong_or_missing_key_provisions_nothing(self):
        for key in ("", "not-the-key"):
            with self.subTest(key=key):
                self.assertEqual(self.provision(key=key).status_code, 401)
        # Counted by prefix, not in total: migration 0006 seeds an account to
        # backfill owners onto pre-ownership rows.
        self.assertEqual(
            get_user_model().objects.filter(username__startswith="partner-").count(), 0,
        )

    @override_settings(PARTNER_API_KEY="")
    def test_an_unset_key_refuses_rather_than_accepting_anything(self):
        self.assertEqual(self.provision(key="").status_code, 401)

    def test_requires_a_partner_and_external_id(self):
        for payload in (
            {"external_id": "x"},
            {"partner": "transwell"},
            {"partner": "transwell", "external_id": "x" * 65},
            {"partner": "../../evil", "external_id": "x"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    self.url, payload, content_type="application/json",
                    HTTP_X_PARTNER_KEY=KEY,
                )
                self.assertEqual(response.status_code, 400)


@override_settings(PARTNER_API_KEY=KEY)
class PartnerAccountIsolationTests(TestCase):
    """A provisioned account is a real account: its rows are its own."""

    def setUp(self):
        self.tokens = {}
        for external_id in ("member-1", "member-2"):
            response = self.client.post(
                reverse("partner-accounts"),
                {
                    "partner": "transwell",
                    "external_id": external_id,
                    "email": f"{external_id}@example.test",
                },
                content_type="application/json",
                HTTP_X_PARTNER_KEY=KEY,
            )
            self.tokens[external_id] = response.json()["token"]

    def as_member(self, external_id):
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Token {self.tokens[external_id]}"
        return self.client

    def test_one_member_never_sees_another_s_skills(self):
        self.as_member("member-1").post(
            "/api/identity/skills/",
            {"name": "Peer support", "category": "Community", "proficiency": "strong"},
            content_type="application/json",
        )

        listed = self.as_member("member-2").get("/api/identity/skills/").json()

        self.assertEqual(listed, [] if isinstance(listed, list) else listed.get("results"))
        self.assertEqual(Skill.objects.count(), 1)

    def test_the_partner_key_alone_reads_nobody_s_data(self):
        self.client.defaults.pop("HTTP_AUTHORIZATION", None)

        response = self.client.get("/api/identity/skills/", HTTP_X_PARTNER_KEY=KEY)

        self.assertEqual(response.status_code, 401)


@override_settings(PARTNER_API_KEY=KEY)
class PartnerDisconnectTests(TestCase):
    def setUp(self):
        self.client.post(
            reverse("partner-accounts"),
            {"partner": "transwell", "external_id": "member-1", "email": "m@example.test"},
            content_type="application/json",
            HTTP_X_PARTNER_KEY=KEY,
        )
        self.user = get_user_model().objects.get(username="partner-transwell-member-1")
        self.url = reverse(
            "partner-account-detail",
            kwargs={"partner": "transwell", "external_id": "member-1"},
        )

    def test_disconnecting_deletes_the_account_and_everything_on_it(self):
        Skill.objects.create(owner=self.user, name="Peer support", proficiency="strong")

        response = self.client.delete(self.url, HTTP_X_PARTNER_KEY=KEY)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertEqual(Skill.objects.count(), 0)
        self.assertEqual(Token.objects.count(), 0)

    def test_disconnecting_twice_is_fine(self):
        self.client.delete(self.url, HTTP_X_PARTNER_KEY=KEY)

        self.assertEqual(
            self.client.delete(self.url, HTTP_X_PARTNER_KEY=KEY).status_code, 204,
        )

    def test_a_wrong_key_deletes_nothing(self):
        self.assertEqual(
            self.client.delete(self.url, HTTP_X_PARTNER_KEY="nope").status_code, 401,
        )
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_it_cannot_be_pointed_at_a_real_person_s_account(self):
        real = get_user_model().objects.create_user(
            username="a-real-person", email="real@example.test", password="x",
        )
        url = reverse(
            "partner-account-detail",
            kwargs={"partner": "transwell", "external_id": "member-1"},
        )
        # The username it resolves is always prefixed, so no combination of
        # partner/external_id reaches "madelyn".
        self.assertNotEqual(partner_username("transwell", "member-1"), real.username)

        self.client.delete(url, HTTP_X_PARTNER_KEY=KEY)

        self.assertTrue(get_user_model().objects.filter(pk=real.pk).exists())


@override_settings(PARTNER_API_KEY=KEY, WORKS_COUT_OWNER_EMAIL="owner@example.test")
class DefaultOwnerStillResolvesTests(TestCase):
    """Machine callers must keep working once member accounts exist.

    `get_default_owner` falls back to "the only user" when
    WORKS_COUT_OWNER_EMAIL is unset — which silently stops being true the first
    time a member connects. The Apify ingestion webhook resolves its owner that
    way, so this is what stands between provisioning and a dead job feed.
    """

    def test_the_owner_is_still_found_after_members_are_provisioned(self):
        owner = get_user_model().objects.create_user(
            username="admin", email="owner@example.test", password="x",
        )
        self.client.post(
            reverse("partner-accounts"),
            {"partner": "transwell", "external_id": "member-1", "email": "m@example.test"},
            content_type="application/json",
            HTTP_X_PARTNER_KEY=KEY,
        )

        self.assertEqual(get_default_owner(), owner)
