import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import JobFilterPreferences
from ingestion.models import IngestedPosting

# Downtown Louisville.
HOME = (38.2527, -85.7585)


class FeedToolsTests(TestCase):
    """
    The workplace, distance, posted-within, search and no-account filters, and
    the feed's sort orders.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tester", password="x")
        cls.token = Token.objects.create(user=cls.user)
        today = datetime.date.today()

        def make(title, **fields):
            return IngestedPosting.objects.create(
                owner=cls.user, source="s", title=title, url=f"https://jobs.test/{title}", **fields)

        # ~11 miles out, in Jeffersontown.
        cls.near = make("Near", company_name="Bluegrass Org", work_arrangement="onsite",
                        latitude=38.1942, longitude=-85.5644, posted_at=today - datetime.timedelta(days=1),
                        salary_min_annual=60000, salary_max_annual=70000, score=50,
                        raw_payload={"descriptionText": "Lead our grant writing program."})
        # ~70 miles out, in Lexington.
        cls.far = make("Far", company_name="Alpha Health", work_arrangement="hybrid",
                       latitude=38.0406, longitude=-84.5037, posted_at=today - datetime.timedelta(days=10),
                       salary_min_annual=90000, salary_max_annual=110000, score=90,
                       apply_url="https://alpha.wd5.myworkdayjobs.com/en-US/careers/job/1")
        # Remote, with an office in San Francisco that shouldn't count as a commute.
        cls.remote = make("Remote", company_name="zeta Labs", work_arrangement="remote", is_remote=True,
                          latitude=37.77, longitude=-122.42, posted_at=today - datetime.timedelta(days=3),
                          score=70)
        # Somewhere, but nobody said where.
        cls.unlocated = make("Unlocated", company_name="Mystery Co", work_arrangement="",
                             posted_at=None, score=30)

    def _get(self, query=""):
        response = self.client.get(reverse("ingestedposting-list") + query,
                                   HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _titles(self, query=""):
        return [row["title"] for row in self._get(query)]

    def _set_home(self):
        JobFilterPreferences.objects.update_or_create(
            owner=self.user, defaults={"home_latitude": HOME[0], "home_longitude": HOME[1],
                                       "home_label": "Louisville"})

    # Workplace

    def test_workplace_filter_takes_several(self):
        self.assertEqual(sorted(self._titles("?workplace=hybrid&workplace=onsite")), ["Far", "Near"])

    def test_workplace_ignores_unknown_tokens(self):
        self.assertEqual(len(self._titles("?workplace=moon")), 4)

    def test_legacy_remote_param_still_works(self):
        self.assertEqual(self._titles("?remote=1"), ["Remote"])

    # Distance

    def test_distance_needs_a_home(self):
        # No home saved: nothing to measure from, so the radius is ignored.
        self.assertEqual(len(self._titles("?within_miles=25")), 4)
        self.assertIsNone(self._get()[0]["distance_miles"])

    def test_radius_keeps_remote_by_default(self):
        self._set_home()
        self.assertEqual(sorted(self._titles("?within_miles=25")), ["Near", "Remote"])

    def test_radius_can_drop_remote(self):
        self._set_home()
        self.assertEqual(self._titles("?within_miles=25&include_remote=0"), ["Near"])

    def test_wider_radius(self):
        self._set_home()
        self.assertEqual(sorted(self._titles("?within_miles=100&include_remote=0")), ["Far", "Near"])

    def test_explicit_coordinates_override_the_saved_home(self):
        # Measured from Lexington, only the Lexington job is close.
        lex = "near_lat=38.0406&near_lng=-84.5037"
        self.assertEqual(self._titles(f"?{lex}&within_miles=10&include_remote=0"), ["Far"])

    def test_distance_is_reported_except_for_remote(self):
        self._set_home()
        rows = {row["title"]: row["distance_miles"] for row in self._get()}
        self.assertAlmostEqual(rows["Near"], 11, delta=2)
        self.assertAlmostEqual(rows["Far"], 70, delta=3)
        self.assertIsNone(rows["Remote"])
        self.assertIsNone(rows["Unlocated"])

    # Posted within, search, account

    def test_posted_within(self):
        # Unlocated has no listing date, so it falls back to the day it was scraped (today).
        self.assertEqual(sorted(self._titles("?posted_within=3")), ["Near", "Remote", "Unlocated"])
        self.assertEqual(sorted(self._titles("?posted_within=1")), ["Near", "Unlocated"])

    def test_search_matches_title_company_and_description(self):
        self.assertEqual(self._titles("?q=grant"), ["Near"])
        self.assertEqual(self._titles("?q=alpha"), ["Far"])
        self.assertEqual(self._titles("?q=GRANT+bluegrass"), ["Near"])
        self.assertEqual(self._titles("?q=grant+alpha"), [])

    def test_no_account_drops_gated_portals(self):
        self.assertNotIn("Far", self._titles("?no_account=1"))
        self.assertEqual(len(self._titles("?no_account=1")), 3)

    # Sorting

    def test_default_sort_is_best_match(self):
        self.assertEqual(self._titles(), ["Far", "Remote", "Near", "Unlocated"])

    def test_sort_newest(self):
        # Unlocated has no listing date, so it counts as listed the day it was scraped.
        self.assertEqual(self._titles("?sort=newest"), ["Unlocated", "Near", "Remote", "Far"])

    def test_sort_pay_puts_unpriced_last(self):
        self.assertEqual(self._titles("?sort=pay"), ["Far", "Near", "Remote", "Unlocated"])

    def test_sort_closest(self):
        self._set_home()
        # Remote and unlocated have no commute; they go last, best match first.
        self.assertEqual(self._titles("?sort=closest"), ["Near", "Far", "Remote", "Unlocated"])

    def test_sort_closest_without_home_falls_back_to_best(self):
        self.assertEqual(self._titles("?sort=closest"), ["Far", "Remote", "Near", "Unlocated"])

    def test_sort_company_ignores_case(self):
        self.assertEqual(self._titles("?sort=company"), ["Far", "Near", "Unlocated", "Remote"])

    def test_filters_and_sort_combine(self):
        self._set_home()
        self.assertEqual(self._titles("?within_miles=100&workplace=onsite&workplace=hybrid&sort=pay"),
                         ["Far", "Near"])

    def test_one_malformed_row_does_not_fail_the_whole_feed(self):
        # Rows like these can already be stored; the feed has to read past them.
        for index, payload in enumerate(("weird", ["a"], 7, {"requirements": 7})):
            IngestedPosting.objects.create(
                owner=self.user, source="s", title=f"Odd {index}",
                url=f"https://jobs.test/odd-{index}", raw_payload=payload)
        titles = self._titles()
        self.assertEqual(len(titles), 8)
        self.assertIn("Near", titles)
        self._titles("?q=grant&sort=newest")

    def test_detail_routes_still_work_with_annotations(self):
        self._set_home()
        url = reverse("ingestedposting-dismiss", args=[self.near.pk])
        response = self.client.post(url, HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()["distance_miles"], 11, delta=2)
