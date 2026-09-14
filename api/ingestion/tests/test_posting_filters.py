from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from ingestion.models import IngestedPosting


class PostingFilterTests(TestCase):
    """
    The Job Feed can be narrowed server-side by salary/remote/job-type/score.
    Overlap semantics for salary and the include-unspecified default matter most:
    a filter that silently dropped every unpriced job would gut the feed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tester", password="x")
        cls.token = Token.objects.create(user=cls.user)
        cls.low = IngestedPosting.objects.create(
            owner=cls.user, source="s", title="Low", url="https://x.test/1",
            salary_min_annual=35000, salary_max_annual=45000,
            is_remote=False, employment_types=["full_time"], score=40)
        cls.high = IngestedPosting.objects.create(
            owner=cls.user, source="s", title="High", url="https://x.test/2",
            salary_min_annual=90000, salary_max_annual=120000,
            is_remote=True, employment_types=["contract"], score=85)
        cls.unpriced = IngestedPosting.objects.create(
            owner=cls.user, source="s", title="Unpriced", url="https://x.test/3",
            salary_min_annual=None, salary_max_annual=None,
            is_remote=False, employment_types=["part_time"], score=60)

    def _get(self, query):
        return self.client.get(
            reverse("ingestedposting-list") + query,
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
        )

    def _titles(self, response):
        return sorted(row["title"] for row in response.json())

    def test_salary_min_includes_unpriced_by_default(self):
        self.assertEqual(self._titles(self._get("?salary_min=80000")), ["High", "Unpriced"])

    def test_salary_min_can_exclude_unpriced(self):
        response = self._get("?salary_min=80000&include_unspecified_salary=0")
        self.assertEqual(self._titles(response), ["High"])

    def test_salary_range_overlap(self):
        # 40k–50k overlaps only the Low band (35–45k).
        response = self._get("?salary_min=40000&salary_max=50000&include_unspecified_salary=0")
        self.assertEqual(self._titles(response), ["Low"])

    def test_remote_only(self):
        self.assertEqual(self._titles(self._get("?remote=1")), ["High"])

    def test_job_type_filter(self):
        self.assertEqual(self._titles(self._get("?job_type=contract")), ["High"])

    def test_min_score(self):
        self.assertEqual(self._titles(self._get("?min_score=70")), ["High"])

    def test_garbage_salary_param_is_ignored_not_500(self):
        response = self._get("?salary_min=notanumber")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)

    def test_serializer_exposes_annualized_salary(self):
        row = next(r for r in self._get("").json() if r["title"] == "High")
        self.assertEqual(row["salary_min_annual"], 90000)
        self.assertEqual(row["salary_max_annual"], 120000)
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["employment_types"], ["contract"])
