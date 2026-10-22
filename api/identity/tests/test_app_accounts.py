"""
Signing up and signing in from the app.

The point of these endpoints is that the app can be signed out of the owner's
account and into someone else's — and that the someone else gets a clean
account, not a view of hers. So the tests that matter most are the ones at the
bottom: what a brand-new account sees.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity import llm
from identity.models import JobFilterPreferences, LLMCredential, ProfessionalProfile, Skill
from identity.owners import NoDefaultOwner, get_default_owner
from ingestion.models import IngestedPosting

EMAIL = "newcomer@example.test"
PASSWORD = "a-long-unguessable-phrase"


class AccountTestCase(TestCase):
    def setUp(self):
        # Throttle counts live in the cache and would otherwise carry over
        # from one test to the next.
        cache.clear()

    def sign_up(self, email=EMAIL, password=PASSWORD, **extra):
        return self.client.post(
            reverse("auth-sign-up"), {"email": email, "password": password},
            content_type="application/json", **extra,
        )

    def sign_in(self, email=EMAIL, password=PASSWORD, **extra):
        return self.client.post(
            reverse("auth-sign-in"), {"email": email, "password": password},
            content_type="application/json", **extra,
        )

    @staticmethod
    def auth(token):
        return {"HTTP_AUTHORIZATION": f"Token {token}"}


class SignUpTests(AccountTestCase):
    def test_creates_an_account_and_returns_its_token(self):
        response = self.sign_up()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        user = get_user_model().objects.get(username=EMAIL)
        self.assertEqual(body["email"], EMAIL)
        self.assertEqual(body["token"], Token.objects.get(user=user).key)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_the_email_is_matched_whatever_its_capitals(self):
        self.sign_up(email="Newcomer@Example.test")

        self.assertEqual(self.sign_up().status_code, 400)
        self.assertEqual(self.sign_in().status_code, 200)

    def test_an_email_already_in_use_is_refused(self):
        self.sign_up()
        response = self.sign_up(password="another-long-phrase-entirely")

        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["email"][0])
        self.assertEqual(get_user_model().objects.filter(email__iexact=EMAIL).count(), 1)

    def test_the_email_on_an_older_account_is_refused_too(self):
        # The owner's account predates sign-up: its username isn't its email.
        get_user_model().objects.create_user("admin", email=EMAIL, password="x")

        self.assertEqual(self.sign_up().status_code, 400)

    def test_a_partner_member_can_also_make_an_account_of_their_own(self):
        # A partner-provisioned account can't be signed in to with a password,
        # so sharing its email must not lock the person out of signing up.
        get_user_model().objects.create_user("partner-transwell-7", email=EMAIL)

        self.assertEqual(self.sign_up().status_code, 201)

    def test_a_weak_password_is_refused_with_the_reason(self):
        response = self.sign_up(password="12345")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["password"])
        self.assertFalse(get_user_model().objects.filter(username=EMAIL).exists())

    def test_missing_or_malformed_details_are_refused(self):
        self.assertIn("email", self.sign_up(email="").json())
        self.assertIn("password", self.sign_up(password="").json())
        self.assertIn("email", self.sign_up(email="not-an-address").json())

    def test_an_email_that_reads_as_a_partner_account_is_refused(self):
        # The partner key may delete any username carrying that prefix.
        response = self.sign_up(email="partner-transwell-9@example.test")

        self.assertEqual(response.status_code, 400)

    def test_a_stale_token_on_the_request_does_not_block_signing_up(self):
        # A signed-out or mid-switch app may still send an Authorization
        # header; the default authentication would answer that with a 401.
        for header in ("Token ", "Token not-a-real-token"):
            with self.subTest(header=header):
                cache.clear()
                response = self.sign_up(
                    email=f"{len(header)}@example.test", HTTP_AUTHORIZATION=header,
                )
                self.assertEqual(response.status_code, 201)


class SignInTests(AccountTestCase):
    def test_returns_the_same_token_the_account_already_has(self):
        token = self.sign_up().json()["token"]

        response = self.sign_in()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], token)

    def test_an_older_account_signs_in_by_username_or_email(self):
        owner = get_user_model().objects.create_user(
            "admin", email="owner@example.test", password=PASSWORD,
        )

        for identifier in ("admin", "owner@example.test", "OWNER@example.test"):
            with self.subTest(identifier=identifier):
                response = self.sign_in(email=identifier)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["token"], Token.objects.get(user=owner).key)

    def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(self):
        self.sign_up()

        wrong_password = self.sign_in(password="not-the-password")
        unknown_email = self.sign_in(email="nobody@example.test")

        self.assertEqual(wrong_password.status_code, 400)
        self.assertEqual(unknown_email.status_code, 400)
        self.assertEqual(wrong_password.json(), unknown_email.json())
        self.assertNotIn("token", wrong_password.json())

    def test_a_partner_account_cannot_be_signed_in_to_with_a_password(self):
        member = get_user_model().objects.create_user("partner-transwell-7", email=EMAIL)
        # Even if one were somehow set, the password route stays closed to it.
        member.set_password(PASSWORD)
        member.save()

        self.assertEqual(self.sign_in().status_code, 400)
        self.assertEqual(self.sign_in(email="partner-transwell-7").status_code, 400)

    def test_a_deactivated_account_cannot_sign_in(self):
        self.sign_up()
        get_user_model().objects.filter(username=EMAIL).update(is_active=False)

        self.assertEqual(self.sign_in().status_code, 400)

    def test_repeated_attempts_are_cut_off(self):
        self.sign_up()

        statuses = [self.sign_in(password="guess").status_code for _ in range(31)]

        self.assertEqual(set(statuses[:30]), {400})
        self.assertEqual(statuses[30], 429)

    def test_a_made_up_forwarded_address_does_not_reset_the_count(self):
        # nginx appends the real address last; that is the one counted.
        statuses = [
            self.sign_in(
                password="guess",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{attempt}, 203.0.113.9",
            ).status_code
            for attempt in range(31)
        ]

        self.assertEqual(statuses[30], 429)


class AccountViewTests(AccountTestCase):
    def test_says_whose_token_this_is(self):
        token = self.sign_up().json()["token"]

        response = self.client.get(reverse("auth-me"), **self.auth(token))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "email": EMAIL, "username": EMAIL, "uses_server_ai_key": False,
        })

    def test_an_account_not_made_here_is_told_the_server_s_key_covers_it(self):
        owner = get_user_model().objects.create_user("admin", password=PASSWORD)
        token = Token.objects.create(user=owner).key

        body = self.client.get(reverse("auth-me"), **self.auth(token)).json()

        self.assertTrue(body["uses_server_ai_key"])

    def test_needs_a_token(self):
        self.assertEqual(self.client.get(reverse("auth-me")).status_code, 401)


class NewAccountStartsCleanTests(AccountTestCase):
    """Nothing of the owner's reaches an account that signs up."""

    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_superuser(
            "admin", email="owner@example.test", password=PASSWORD,
        )
        ProfessionalProfile.objects.create(
            owner=self.owner, headline="Director of Development", master_resume="Her history.",
        )
        Skill.objects.create(owner=self.owner, name="Fundraising", proficiency="expert")
        JobFilterPreferences.objects.create(
            owner=self.owner, salary=False, match_score=True, sort="pay",
            home_label="Louisville, KY", home_latitude=38.25, home_longitude=-85.76,
            radius_miles=60,
        )
        IngestedPosting.objects.create(
            owner=self.owner, source="apify:indeed", title="Development Director",
            url="https://jobs.example.test/1", score=92,
            score_reasons=["Matches your development lane"],
        )
        self.newcomer = self.auth(self.sign_up().json()["token"])

    def test_it_has_no_profile_skills_resumes_or_links(self):
        for name in ("professionalprofile-list", "skill-list", "resumeversion-list",
                     "profilelink-list", "llmcredential-list"):
            with self.subTest(endpoint=name):
                response = self.client.get(reverse(name), **self.newcomer)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])

    def test_its_filter_choices_are_the_defaults_not_the_owner_s(self):
        body = self.client.get(reverse("filter-preferences"), **self.newcomer).json()

        self.assertTrue(body["salary"])
        self.assertFalse(body["match_score"])
        self.assertEqual(body["sort"], "best")
        self.assertEqual(body["radius_miles"], 25)
        self.assertEqual(body["home_label"], "")
        self.assertIsNone(body["home_latitude"])
        self.assertIsNone(body["home_longitude"])

    @override_settings(WORKS_COUT_OWNER_EMAIL="owner@example.test")
    def test_its_job_feed_holds_none_of_the_owner_s_scoring(self):
        # It starts from a copy of the current feed, so it isn't empty, but
        # every copy is ranked against the newcomer's profile, not the owner's.
        rows = self.client.get(reverse("ingestedposting-list"), **self.newcomer).json()

        self.assertEqual([row["title"] for row in rows], ["Development Director"])
        self.assertNotEqual(rows[0]["score"], 92)
        self.assertNotIn("Matches your development lane", rows[0]["score_reasons"])
        self.assertNotIn("Fundraising", str(rows[0]["score_reasons"]))

    def test_signing_up_leaves_the_owner_s_own_view_untouched(self):
        owner = self.auth(self.sign_in(email="owner@example.test").json()["token"])

        skills = self.client.get(reverse("skill-list"), **owner).json()
        postings = self.client.get(reverse("ingestedposting-list"), **owner).json()

        self.assertEqual([row["name"] for row in skills], ["Fundraising"])
        self.assertEqual([row["title"] for row in postings], ["Development Director"])


@override_settings(ANTHROPIC_API_KEY="the-server-key")
class NewAccountBringsItsOwnKeyTests(AccountTestCase):
    """Anyone can sign up, so the server's AI key is not theirs to spend."""

    def setUp(self):
        super().setUp()
        self.token = self.sign_up().json()["token"]
        self.user = get_user_model().objects.get(username=EMAIL)

    def give_key(self, raw="sk-their-own"):
        cred = LLMCredential(owner=self.user, provider="anthropic", is_active=True)
        cred.set_key(raw)
        cred.save()
        return cred

    @patch("identity.llm._complete_anthropic", return_value="ANTHROPIC")
    def test_without_a_key_nothing_is_sent_on_the_server_s(self, mock_anthropic):
        with self.assertRaises(llm.LLMUnavailable) as raised:
            llm.complete("sys", "user", config=llm.resolve_config(self.user))

        self.assertIn("Add your own API key", str(raised.exception))
        mock_anthropic.assert_not_called()

    @patch("identity.llm._complete_anthropic", return_value="ANTHROPIC")
    def test_a_document_review_asks_for_a_key_instead_of_using_the_server_s(self, mock_anthropic):
        response = self.client.post(
            reverse("review-document"), {"text": "Ten years of nonprofit development work. " * 10},
            content_type="application/json", **self.auth(self.token),
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Add your own API key", response.json()["detail"])
        mock_anthropic.assert_not_called()

    @patch("identity.llm._complete_anthropic", return_value="ANTHROPIC")
    def test_with_its_own_key_the_call_runs_on_that_key(self, mock_anthropic):
        self.give_key()

        llm.complete("sys", "user", config=llm.resolve_config(self.user))

        self.assertEqual(mock_anthropic.call_args.kwargs["api_key"], "sk-their-own")

    def test_a_key_that_can_t_be_read_does_not_fall_back_to_the_server_s(self):
        cred = self.give_key()
        LLMCredential.objects.filter(pk=cred.pk).update(api_key_encrypted="not-a-ciphertext")

        self.assertEqual(llm.resolve_config(self.user), llm.NO_KEY)

    def test_the_owner_and_partner_members_keep_the_server_s_key(self):
        User = get_user_model()
        owner = User.objects.create_superuser("admin", password=PASSWORD)
        member = User.objects.create_user("partner-transwell-7")

        self.assertIsNone(llm.resolve_config(owner))
        self.assertIsNone(llm.resolve_config(member))

    def test_taking_an_account_out_of_the_group_gives_it_the_server_s_key(self):
        # How she lets one person through, from the admin.
        self.user.groups.clear()

        self.assertIsNone(llm.resolve_config(self.user))


class DefaultOwnerSurvivesSignUpsTests(AccountTestCase):
    """Ingestion and the shared feed resolve the owner without a request.

    With WORKS_COUT_OWNER_EMAIL unset that used to mean "the only user", which
    the first sign-up would have broken — taking the job feed down with it.
    """

    def setUp(self):
        super().setUp()
        # Migration 0006 seeds an owner account; start from none so each test
        # decides who exists.
        get_user_model().objects.all().delete()

    @override_settings(WORKS_COUT_OWNER_EMAIL="")
    def test_the_one_superuser_is_the_owner_once_others_have_signed_up(self):
        owner = get_user_model().objects.create_superuser("admin", password=PASSWORD)
        self.sign_up()

        self.assertEqual(get_default_owner(), owner)

    @override_settings(WORKS_COUT_OWNER_EMAIL="")
    def test_no_owner_is_guessed_when_no_superuser_stands_out(self):
        self.sign_up()
        self.sign_up(email="second@example.test")

        with self.assertRaises(NoDefaultOwner):
            get_default_owner()
