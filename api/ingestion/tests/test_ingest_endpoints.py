from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from ingestion.models import IngestedPosting
from ingestion.services import ingest_items

TEST_KEY = "test-ingestion-key"


@override_settings(INGESTION_API_KEY=TEST_KEY)
class IngestViewTests(TestCase):
    def setUp(self):
        self.url = reverse("ingest")

    def test_accepts_a_single_posting(self):
        response = self.client.post(
            self.url,
            data={"source": "email", "title": "Analyst", "company_name": "Acme"},
            content_type="application/json",
            HTTP_X_INGESTION_KEY=TEST_KEY,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(IngestedPosting.objects.count(), 1)

    def test_accepts_a_batch(self):
        response = self.client.post(
            self.url,
            data=[
                {"source": "email", "title": "Analyst"},
                {"source": "email", "title": "Coordinator"},
            ],
            content_type="application/json",
            HTTP_X_INGESTION_KEY=TEST_KEY,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(IngestedPosting.objects.count(), 2)

    def test_url_stays_optional(self):
        # Email-sourced postings have no URL. The (source, url) unique constraint
        # must not make DRF demand one.
        response = self.client.post(
            self.url,
            data={"source": "email", "title": "Analyst"},
            content_type="application/json",
            HTTP_X_INGESTION_KEY=TEST_KEY,
        )
        self.assertEqual(response.status_code, 201)

    def test_resending_the_same_posting_returns_409_not_500(self):
        payload = {"source": "apify:indeed", "title": "Analyst", "url": "https://x.test/1"}
        first = self.client.post(
            self.url, data=payload, content_type="application/json", HTTP_X_INGESTION_KEY=TEST_KEY
        )
        second = self.client.post(
            self.url, data=payload, content_type="application/json", HTTP_X_INGESTION_KEY=TEST_KEY
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(IngestedPosting.objects.count(), 1)

    def test_rejects_a_bad_key(self):
        response = self.client.post(
            self.url,
            data={"source": "email", "title": "Analyst"},
            content_type="application/json",
            HTTP_X_INGESTION_KEY="wrong",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(IngestedPosting.objects.count(), 0)


class PostingStatusFilterTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("tester", password="x")
        self.token = Token.objects.create(user=user)
        IngestedPosting.objects.create(source="apify:indeed", title="New one", url="https://x.test/1")
        IngestedPosting.objects.create(
            source="apify:indeed",
            title="Old one",
            url="https://x.test/2",
            status=IngestedPosting.Status.DISMISSED,
        )

    def test_filters_by_status(self):
        # The Job Feed asks for new postings only, so it doesn't have to pull
        # every posting ever scraped once daily runs pile up.
        response = self.client.get(
            reverse("ingestedposting-list") + "?status=new",
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
        )
        self.assertEqual(response.status_code, 200)
        titles = [row["title"] for row in response.json()]
        self.assertEqual(titles, ["New one"])

    def test_returns_everything_without_the_filter(self):
        response = self.client.get(
            reverse("ingestedposting-list"),
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
        )
        self.assertEqual(len(response.json()), 2)


class CrossSourceDedupeTests(TestCase):
    """
    The six per-lane Apify searches overlap: "Program Manager" comes back from
    both the programs and development queries. Deduping on (source, url) stored
    that twice and promoted it twice, giving her two applications for one job.
    """

    def test_the_same_url_from_a_different_search_is_not_stored_twice(self):
        item = {"positionName": "Program Manager", "companyName": "Canon",
                "url": "https://example.test/job/canon-pm"}

        first = ingest_items([item], source="apify:indeed-programs")
        second = ingest_items([item], source="apify:indeed-development")

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(IngestedPosting.objects.filter(url=item["url"]).count(), 1)

    def test_the_database_refuses_a_duplicate_url_outright(self):
        IngestedPosting.objects.create(
            source="apify:indeed-programs", title="Program Manager",
            company_name="Canon", url="https://example.test/job/x")
        with self.assertRaises(IntegrityError):
            IngestedPosting.objects.create(
                source="apify:indeed-development", title="Program Manager",
                company_name="Canon", url="https://example.test/job/x")

    def test_blank_urls_do_not_collide_with_each_other(self):
        # The constraint is partial for exactly this reason — plenty of scraped
        # items have no resolvable URL and they're all legitimately distinct.
        for i in range(3):
            IngestedPosting.objects.create(
                source="apify:indeed", title=f"Role {i}", company_name="Co", url="")
        self.assertEqual(IngestedPosting.objects.filter(url="").count(), 3)
