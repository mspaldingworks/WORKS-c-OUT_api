"""
One-time sign-in links.

Each of these pins a property that is easy to lose in a refactor and expensive
to lose in production: the stored token is a hash, a link works once, a link
expires, asking for one never reveals whether an account exists, and a caller
can ask for a link but can never say where it goes.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from identity.magic_links import (
    MAX_OUTSTANDING,
    hash_token,
    issue,
    redeem,
    request_link_for_email,
    send_link,
)
from identity.models import MagicLinkToken, ResumeVersion, Skill

EMAIL_SETTINGS = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "DEFAULT_FROM_EMAIL": "admin@example.test",
    "ACCOUNT_BASE_URL": "https://api.workscout.agency",
}


def make_user(username="partner-transwell-abc", email="river@example.test"):
    user = get_user_model().objects.create_user(username=username, email=email)
    user.set_unusable_password()
    user.save()
    return user


@override_settings(**EMAIL_SETTINGS)
class TokenTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_only_a_hash_of_the_token_is_stored(self):
        token = issue(self.user)

        link = MagicLinkToken.objects.get()
        self.assertNotEqual(link.token_hash, token)
        self.assertEqual(link.token_hash, hash_token(token))
        # A database copy must not contain anything that signs someone in.
        self.assertNotIn(token, str(link.__dict__))

    def test_a_link_works_once(self):
        token = issue(self.user)

        self.assertEqual(redeem(token), self.user)
        self.assertIsNone(redeem(token))

    def test_an_expired_link_does_not_work(self):
        token = issue(self.user)
        MagicLinkToken.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertIsNone(redeem(token))

    def test_a_made_up_token_does_not_work(self):
        issue(self.user)

        self.assertIsNone(redeem("not-a-real-token"))
        self.assertIsNone(redeem(""))
        self.assertIsNone(redeem(None))

    def test_asking_again_keeps_the_earlier_link_working(self):
        first = issue(self.user)
        issue(self.user)

        # Someone who asks twice and clicks the first email should not be told
        # it's broken.
        self.assertEqual(redeem(first), self.user)

    def test_outstanding_links_are_capped(self):
        for _ in range(MAX_OUTSTANDING + 4):
            issue(self.user)

        self.assertEqual(
            MagicLinkToken.objects.filter(used_at__isnull=True).count(),
            MAX_OUTSTANDING,
        )

    def test_one_account_s_link_never_signs_in_another(self):
        other = make_user(username="partner-transwell-def", email="sky@example.test")
        token = issue(other)

        self.assertEqual(redeem(token), other)


@override_settings(**EMAIL_SETTINGS)
class SendingTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_the_email_carries_a_working_link(self):
        self.assertTrue(send_link(self.user.email, self.user))

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("https://api.workscout.agency/account/sign-in/", body)
        token = body.split("/account/sign-in/")[1].split("/")[0]
        self.assertEqual(redeem(token), self.user)

    def test_it_goes_to_the_account_s_address(self):
        send_link(self.user.email, self.user)

        self.assertEqual(mail.outbox[0].to, ["river@example.test"])

    def test_requesting_by_email_finds_the_account_case_insensitively(self):
        request_link_for_email("RIVER@EXAMPLE.TEST")

        self.assertEqual(len(mail.outbox), 1)

    def test_requesting_for_an_unknown_address_sends_nothing_and_says_nothing(self):
        # No exception, no signal — whether an address has an account here is
        # not something an unauthenticated request may find out.
        self.assertIsNone(request_link_for_email("nobody@example.test"))
        self.assertEqual(len(mail.outbox), 0)

    def test_an_account_with_no_email_gets_no_link(self):
        self.user.email = ""
        self.user.save(update_fields=["email"])

        self.assertFalse(send_link(self.user.email, self.user))
        self.assertEqual(MagicLinkToken.objects.count(), 0)

    @patch("identity.magic_links.send_mail", side_effect=OSError("smtp down"))
    def test_a_send_failure_is_reported_rather_than_raised(self, _):
        self.assertFalse(send_link(self.user.email, self.user))


@override_settings(**EMAIL_SETTINGS)
class SignInPageTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_the_sign_in_page_asks_for_an_email(self):
        response = self.client.get(reverse("account-sign-in"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Email me a link")

    def test_posting_a_known_address_sends_a_link(self):
        response = self.client.post(
            reverse("account-sign-in"), {"email": "river@example.test"},
        )

        self.assertContains(response, "Check your email")
        self.assertEqual(len(mail.outbox), 1)

    def test_an_unknown_address_gets_the_identical_page(self):
        known = self.client.post(
            reverse("account-sign-in"), {"email": "river@example.test"},
        )
        unknown = self.client.post(
            reverse("account-sign-in"), {"email": "nobody@example.test"},
        )

        # Byte-identical: the page must not hint at which addresses exist.
        self.assertEqual(known.content, unknown.content)
        self.assertEqual(len(mail.outbox), 1)

    def test_clicking_the_link_signs_you_in(self):
        token = issue(self.user)

        response = self.client.get(
            reverse("account-consume", args=[token]), follow=True,
        )

        self.assertContains(response, "Signed in as river@example.test")

    def test_a_spent_link_says_so_instead_of_erroring(self):
        token = issue(self.user)
        self.client.get(reverse("account-consume", args=[token]))
        self.client.logout()

        response = self.client.get(reverse("account-consume", args=[token]))

        self.assertContains(response, "doesn't work any more")

    def test_the_account_page_needs_a_session(self):
        response = self.client.get(reverse("account-home"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/account/sign-in/", response["Location"])


@override_settings(**EMAIL_SETTINGS)
class AccountPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        Skill.objects.create(owner=self.user, name="Peer support", proficiency="strong")
        self.client.get(reverse("account-consume", args=[issue(self.user)]))

    def test_it_lists_what_is_on_the_account(self):
        response = self.client.get(reverse("account-home"))

        self.assertContains(response, "Peer support")
        self.assertContains(response, "No documents on this account.")

    def test_a_partner_account_is_told_where_it_came_from(self):
        response = self.client.get(reverse("account-home"))

        self.assertContains(response, "when you connected the Jobs tab in")

    def test_it_shows_nobody_else_s_rows(self):
        other = make_user(username="partner-transwell-def", email="sky@example.test")
        Skill.objects.create(owner=other, name="Welding", proficiency="strong")

        response = self.client.get(reverse("account-home"))

        self.assertContains(response, "Peer support")
        self.assertNotContains(response, "Welding")

    def test_deleting_the_account_really_deletes_it(self):
        response = self.client.post(reverse("account-delete"), follow=True)

        self.assertContains(response, "has been deleted")
        self.assertFalse(
            get_user_model().objects.filter(pk=self.user.pk).exists()
        )
        self.assertEqual(Skill.objects.count(), 0)

    def test_signing_out_ends_the_session(self):
        self.client.post(reverse("account-sign-out"))

        self.assertEqual(self.client.get(reverse("account-home")).status_code, 302)

    def test_deleting_someone_else_s_document_is_not_possible(self):
        other = make_user(username="partner-transwell-def", email="sky@example.test")
        theirs = ResumeVersion.objects.create(owner=other, title="Their resume")

        self.client.post(reverse("account-delete-resume", args=[theirs.id]))

        self.assertTrue(ResumeVersion.objects.filter(pk=theirs.pk).exists())


@override_settings(PARTNER_API_KEY="partner-key", **EMAIL_SETTINGS)
class PartnerSignInLinkTests(TestCase):
    """TransWell can have a link sent, but can't choose where it goes."""

    def setUp(self):
        self.user = make_user()
        self.url = reverse(
            "partner-account-sign-in-link",
            kwargs={"partner": "transwell", "external_id": "abc"},
        )

    def test_it_emails_the_member(self):
        response = self.client.post(self.url, HTTP_X_PARTNER_KEY="partner-key")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sent"])
        self.assertEqual(mail.outbox[0].to, ["river@example.test"])

    def test_the_destination_cannot_be_overridden(self):
        self.client.post(
            self.url,
            {"email": "attacker@example.test"},
            content_type="application/json",
            HTTP_X_PARTNER_KEY="partner-key",
        )

        self.assertEqual(mail.outbox[0].to, ["river@example.test"])

    def test_a_wrong_key_sends_nothing(self):
        response = self.client.post(self.url, HTTP_X_PARTNER_KEY="nope")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unknown_member_is_a_404_not_a_silent_success(self):
        url = reverse(
            "partner-account-sign-in-link",
            kwargs={"partner": "transwell", "external_id": "nobody"},
        )

        response = self.client.post(url, HTTP_X_PARTNER_KEY="partner-key")

        self.assertEqual(response.status_code, 404)
