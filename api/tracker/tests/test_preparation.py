from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ProfessionalProfile
from ingestion.models import IngestedPosting
from tracker.models import Application
from tracker.preparation import get_job, prepare_postings

MATERIALS = {
    "cover_letter": "Dear Hiring Team, ...",
    "resume_summary": "Development leader.",
    "resume_bullets": ["Grew Give for Good to $17,000"],
    "gaps": ["No PMP certification."],
    "unparsed": False,
}


def make_posting(url, title="Director of Development", score=90):
    return IngestedPosting.objects.create(
        source="apify:indeed",
        title=title,
        company_name="American Heart Association",
        url=url,
        apply_url=url + "/apply",
        score=score,
        raw_payload={"descriptionText": "x" * 500},
    )


@override_settings(ANTHROPIC_API_KEY="test-key", GOOGLE_SERVICE_ACCOUNT_FILE="", JOB_SHEET_ID="")
class PreparePostingsTests(TestCase):
    """prepare_postings runs its work on a thread; these call the body directly."""

    def setUp(self):
        ProfessionalProfile.objects.create(headline="Director", master_resume="Her background.")

    def run_prepare(self, posting_ids):
        # Run the thread body synchronously so assertions aren't racing it.
        with patch("tracker.preparation.threading.Thread") as thread:
            job = prepare_postings(posting_ids)
            target, args = thread.call_args.kwargs.get("target"), thread.call_args.kwargs.get("args")
            if target is None:
                target, args = thread.call_args[1]["target"], thread.call_args[1]["args"]
            target(*args)
        return get_job(job["id"])

    def test_creates_ready_applications_with_materials(self):
        posting = make_posting("https://example.test/job/1")
        with patch("ingestion.generation.generate_materials", return_value=MATERIALS) as gen:
            job = self.run_prepare([posting.pk])

        self.assertEqual(job["state"], "finished")
        self.assertEqual(job["done"], 1)
        self.assertTrue(job["results"][0]["ok"])
        gen.assert_called_once()

        application = Application.objects.get()
        self.assertEqual(application.status, Application.Status.READY)
        self.assertEqual(application.source_posting, posting)
        posting.refresh_from_db()
        self.assertEqual(posting.generated_materials["cover_letter"], MATERIALS["cover_letter"])

    def test_does_not_pay_to_regenerate_existing_materials(self):
        posting = make_posting("https://example.test/job/2")
        posting.generated_materials = MATERIALS
        posting.save(update_fields=["generated_materials"])

        with patch("ingestion.generation.generate_materials") as gen:
            self.run_prepare([posting.pk])

        gen.assert_not_called()

    def test_one_failure_does_not_lose_the_rest_of_the_batch(self):
        good = make_posting("https://example.test/job/3")
        missing_id = 999999
        with patch("ingestion.generation.generate_materials", return_value=MATERIALS):
            job = self.run_prepare([missing_id, good.pk])

        self.assertEqual(job["done"], 2)
        outcomes = {r["posting_id"]: r["ok"] for r in job["results"]}
        self.assertFalse(outcomes[missing_id])
        self.assertTrue(outcomes[good.pk])
        self.assertEqual(Application.objects.count(), 1)

    def test_queues_the_job_even_when_generation_is_unavailable(self):
        # A posting with no letter is still worth tracking; she can write one later.
        posting = make_posting("https://example.test/job/4")
        from ingestion.generation import GenerationUnavailable

        with patch("ingestion.generation.generate_materials", side_effect=GenerationUnavailable("no key")):
            job = self.run_prepare([posting.pk])

        self.assertTrue(job["results"][0]["ok"])
        self.assertIn("no key", job["results"][0]["detail"])
        self.assertEqual(Application.objects.get().status, Application.Status.READY)


@override_settings(ANTHROPIC_API_KEY="test-key", GOOGLE_SERVICE_ACCOUNT_FILE="", JOB_SHEET_ID="")
class ApplicationEndpointTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}
        self.posting = make_posting("https://example.test/job/5")

    def test_prepare_returns_a_job_id_without_blocking(self):
        url = reverse("application-prepare")
        with patch("tracker.preparation.threading.Thread"):
            response = self.client.post(url, {"posting_ids": [self.posting.pk]},
                                        content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["state"], "running")

    def test_prepare_rejects_an_empty_list(self):
        url = reverse("application-prepare")
        response = self.client.post(url, {"posting_ids": []},
                                    content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 400)

    def test_mark_applied_sets_the_date(self):
        from ingestion.services import promote_posting_to_application

        application = promote_posting_to_application(self.posting)
        url = reverse("application-mark-applied", args=[application.pk])
        response = self.client.post(url, **self.auth)

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.APPLIED)
        self.assertIsNotNone(application.applied_date)

    def test_serializer_exposes_the_apply_url_and_materials(self):
        from ingestion.services import promote_posting_to_application

        self.posting.generated_materials = MATERIALS
        self.posting.save(update_fields=["generated_materials"])
        promote_posting_to_application(self.posting)

        response = self.client.get(reverse("application-list"), **self.auth)
        row = response.json()[0]
        self.assertEqual(row["apply_url"], self.posting.apply_url)
        self.assertEqual(row["generated_materials"]["cover_letter"], MATERIALS["cover_letter"])

    def test_requires_authentication(self):
        self.assertEqual(self.client.post(reverse("application-prepare")).status_code, 401)


@override_settings(ANTHROPIC_API_KEY="test-key")
class ReviewAndApproveTests(TestCase):
    """The review loop: read the draft, edit it, approve it, then mark applied."""

    def setUp(self):
        from ingestion.services import promote_posting_to_application

        user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}
        ProfessionalProfile.objects.create(legal_name="Madelyn Spalding", master_resume="Background.")
        self.posting = make_posting("https://example.test/job/review")
        self.posting.generated_materials = MATERIALS
        self.posting.save(update_fields=["generated_materials"])
        self.application = promote_posting_to_application(self.posting)

    def test_approving_records_it_without_sending_anything(self):
        response = self.client.post(
            reverse("application-approve", args=[self.application.pk]), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.APPROVED)
        # Approval is a note to herself; nothing about the posting changes.
        self.posting.refresh_from_db()
        self.assertEqual(self.posting.generated_materials, MATERIALS)

    def test_her_edits_replace_the_generated_text(self):
        response = self.client.patch(
            reverse("application-edit-materials", args=[self.application.pk]),
            {"cover_letter": "My own words.", "gaps": ["Only this one."]},
            content_type="application/json", **self.auth)

        self.assertEqual(response.status_code, 200)
        self.posting.refresh_from_db()
        self.assertEqual(self.posting.generated_materials["cover_letter"], "My own words.")
        self.assertEqual(self.posting.generated_materials["gaps"], ["Only this one."])
        # Untouched fields survive.
        self.assertEqual(self.posting.generated_materials["resume_summary"],
                         MATERIALS["resume_summary"])

    def test_editing_rejects_a_bullet_list_that_is_not_a_list(self):
        response = self.client.patch(
            reverse("application-edit-materials", args=[self.application.pk]),
            {"resume_bullets": "not a list"},
            content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 400)

    def test_serializer_exposes_what_the_review_screen_needs(self):
        response = self.client.get(reverse("application-list"), **self.auth)
        row = next(r for r in response.json() if r["id"] == self.application.pk)
        for field in ("apply_url", "generated_materials", "resume_drive_url",
                      "cover_letter_drive_url", "status"):
            self.assertIn(field, row)

    def test_an_application_without_materials_reports_null_not_empty(self):
        # An empty object claims to be materials while missing every field. The
        # iOS client decodes this into a typed optional, and one such row broke
        # the whole Drafts screen with "decoding failed".
        from tracker.models import Application

        bare = Application.objects.create(
            company=self.application.company, role_title="Legacy row")
        response = self.client.get(reverse("application-list"), **self.auth)
        row = next(r for r in response.json() if r["id"] == bare.pk)
        self.assertIsNone(row["generated_materials"])

    def test_requires_authentication(self):
        for name in ("application-approve", "application-edit-materials"):
            self.assertIn(
                self.client.post(reverse(name, args=[self.application.pk])).status_code,
                (401, 405),
            )


@override_settings(ANTHROPIC_API_KEY="test-key")
class RemoveAndUndoTests(TestCase):
    """
    Removing is reversible by design — the app's own rules (CLAUDE.md §3.5) ask
    for undo rather than a confirmation dialog, because dialogs get dismissed
    reflexively and undo actually protects the data.
    """

    def setUp(self):
        from ingestion.services import promote_posting_to_application

        user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}
        self.posting = make_posting("https://example.test/job/remove")
        self.posting.generated_materials = MATERIALS
        self.posting.save(update_fields=["generated_materials"])
        self.application = promote_posting_to_application(self.posting)

    def test_discarding_keeps_the_record_and_its_materials(self):
        response = self.client.post(
            reverse("application-discard", args=[self.application.pk]), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.DISCARDED)
        # The generated text cost money; removing it from a list must not burn it.
        self.posting.refresh_from_db()
        self.assertEqual(self.posting.generated_materials, MATERIALS)

    def test_discarding_also_takes_the_posting_out_of_the_feed(self):
        # Otherwise tomorrow's feed shows the job she just removed.
        self.client.post(reverse("application-discard", args=[self.application.pk]), **self.auth)
        self.posting.refresh_from_db()
        self.assertEqual(self.posting.status, IngestedPosting.Status.DISMISSED)

    def test_restore_puts_it_back(self):
        self.client.post(reverse("application-discard", args=[self.application.pk]), **self.auth)
        self.client.post(reverse("application-restore", args=[self.application.pk]), **self.auth)

        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.READY)

    def test_dismissing_a_posting_removes_it_from_the_feed_and_restore_returns_it(self):
        posting = make_posting("https://example.test/job/feed-remove")
        feed = reverse("ingestedposting-list") + "?status=new"

        self.client.post(reverse("ingestedposting-dismiss", args=[posting.pk]), **self.auth)
        ids = [row["id"] for row in self.client.get(feed, **self.auth).json()]
        self.assertNotIn(posting.pk, ids)

        self.client.post(reverse("ingestedposting-restore", args=[posting.pk]), **self.auth)
        ids = [row["id"] for row in self.client.get(feed, **self.auth).json()]
        self.assertIn(posting.pk, ids)

    def test_removing_requires_authentication(self):
        for name, args in (("application-discard", [self.application.pk]),
                           ("ingestedposting-dismiss", [self.posting.pk])):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name, args=args)).status_code, 401)
