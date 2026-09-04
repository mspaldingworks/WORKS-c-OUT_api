from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from ingestion.models import IngestedPosting

WEBHOOK_PAYLOAD = {
    "eventType": "ACTOR.RUN.SUCCEEDED",
    "resource": {"id": "run123", "defaultDatasetId": "dataset123", "status": "SUCCEEDED"},
}

DATASET_ITEMS = [
    {
        "positionName": "Senior Program Manager",
        "company": "Acme Nonprofit",
        "jobUrl": "https://www.indeed.com/viewjob?jk=abc123",
    },
    {
        "positionName": "Development Coordinator",
        "company": "Initech Foundation",
        "jobUrl": "https://www.indeed.com/viewjob?jk=def456",
    },
]

TEST_KEY = "test-ingestion-key"


@override_settings(INGESTION_API_KEY=TEST_KEY)
class ApifyWebhookTests(TestCase):
    def setUp(self):
        self.url = reverse("apify-webhook")

    def _post(self, payload=WEBHOOK_PAYLOAD, key=TEST_KEY, source="indeed", use_header=False):
        query = f"?source={source}"
        headers = {}
        if key is not None:
            if use_header:
                headers["HTTP_X_INGESTION_KEY"] = key
            else:
                query += f"&key={key}"
        return self.client.post(
            f"{self.url}{query}", data=payload, content_type="application/json", **headers
        )

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_ingests_dataset_items(self, mock_fetch):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 2)
        self.assertEqual(IngestedPosting.objects.count(), 2)
        mock_fetch.assert_called_once()

        posting = IngestedPosting.objects.get(url="https://www.indeed.com/viewjob?jk=abc123")
        self.assertEqual(posting.title, "Senior Program Manager")
        self.assertEqual(posting.company_name, "Acme Nonprofit")
        self.assertEqual(posting.source, "apify:indeed")
        self.assertEqual(posting.status, IngestedPosting.Status.NEW)

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_accepts_key_via_header_too(self, _mock_fetch):
        response = self._post(use_header=True)
        self.assertEqual(response.status_code, 200)

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_rerunning_the_same_scrape_creates_nothing_new(self, _mock_fetch):
        self._post()
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 0)
        self.assertEqual(response.json()["duplicates"], 2)
        self.assertEqual(IngestedPosting.objects.count(), 2)

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_dismissed_postings_do_not_come_back(self, _mock_fetch):
        self._post()
        IngestedPosting.objects.update(status=IngestedPosting.Status.DISMISSED)

        self._post()

        self.assertEqual(IngestedPosting.objects.count(), 2)
        self.assertFalse(IngestedPosting.objects.filter(status=IngestedPosting.Status.NEW).exists())

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_same_url_from_a_different_board_is_deduped(self, _mock_fetch):
        # The six per-lane searches overlap heavily, so the same job arrives
        # from several of them. Keeping one copy per board meant duplicate
        # postings in the feed and, once promoted, duplicate applications.
        self._post(source="indeed")
        self._post(source="linkedin")
        self.assertEqual(IngestedPosting.objects.count(), len(DATASET_ITEMS))

    @patch("ingestion.views.fetch_dataset_items")
    def test_duplicates_within_a_single_batch_are_collapsed(self, mock_fetch):
        mock_fetch.return_value = [DATASET_ITEMS[0], DATASET_ITEMS[0]]
        response = self._post()
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(IngestedPosting.objects.count(), 1)

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_rejects_missing_or_wrong_key(self, _mock_fetch):
        self.assertEqual(self._post(key=None).status_code, 401)
        self.assertEqual(self._post(key="wrong").status_code, 401)
        self.assertEqual(IngestedPosting.objects.count(), 0)

    @patch("ingestion.views.fetch_dataset_items", return_value=DATASET_ITEMS)
    def test_ignores_non_success_events_without_asking_apify_to_retry(self, mock_fetch):
        response = self._post(payload={"eventType": "ACTOR.RUN.FAILED", "resource": {}})

        self.assertEqual(response.status_code, 200)  # non-2xx would make Apify retry
        mock_fetch.assert_not_called()
        self.assertEqual(IngestedPosting.objects.count(), 0)

    def test_rejects_payload_without_a_dataset_id(self):
        response = self._post(payload={"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {}})
        self.assertEqual(response.status_code, 400)

    @patch("ingestion.views.fetch_dataset_items", side_effect=OSError("apify down"))
    def test_returns_502_so_apify_retries_a_transient_fetch_failure(self, _mock_fetch):
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(IngestedPosting.objects.count(), 0)
