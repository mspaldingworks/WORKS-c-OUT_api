from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import JobFilterPreferences


class FilterPreferencesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")
        self.token = Token.objects.create(user=self.user)
        self.url = reverse("filter-preferences")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Token {self.token.key}"}

    def test_get_autocreates_with_defaults(self):
        self.assertEqual(JobFilterPreferences.objects.count(), 0)
        response = self.client.get(self.url, **self._auth())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Salary, workplace, distance and posting date on by default; the
        # narrower ones off.
        self.assertTrue(body["salary"])
        self.assertTrue(body["remote"])
        self.assertTrue(body["distance"])
        self.assertTrue(body["posted_date"])
        self.assertFalse(body["job_type"])
        self.assertFalse(body["match_score"])
        self.assertFalse(body["easy_apply"])
        self.assertEqual(body["sort"], "best")
        self.assertEqual(body["radius_miles"], 25)
        self.assertIsNone(body["home_latitude"])
        self.assertEqual(JobFilterPreferences.objects.filter(owner=self.user).count(), 1)

    def test_patch_toggles_and_persists(self):
        response = self.client.patch(
            self.url, data={"remote": True, "salary": False},
            content_type="application/json", **self._auth())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["remote"])
        self.assertFalse(body["salary"])
        prefs = JobFilterPreferences.objects.get(owner=self.user)
        self.assertTrue(prefs.remote)
        self.assertFalse(prefs.salary)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_is_scoped_to_the_caller(self):
        other = get_user_model().objects.create_user("other", password="x")
        JobFilterPreferences.objects.create(owner=other, remote=False, home_label="Elsewhere",
                                            home_latitude=10.0, home_longitude=10.0)
        # My row is created fresh with defaults, not read from someone else's.
        body = self.client.get(self.url, **self._auth()).json()
        self.assertTrue(body["remote"])
        self.assertEqual(body["home_label"], "")

    def _patch(self, data):
        return self.client.patch(self.url, data=data, content_type="application/json", **self._auth())

    def test_saves_home_location_radius_and_sort(self):
        response = self._patch({"home_label": "Louisville, KY 40205", "home_latitude": 38.22,
                                "home_longitude": -85.69, "radius_miles": 40, "sort": "closest"})
        self.assertEqual(response.status_code, 200, response.content)
        prefs = JobFilterPreferences.objects.get(owner=self.user)
        self.assertEqual(prefs.home, (38.22, -85.69))
        self.assertEqual(prefs.radius_miles, 40)
        self.assertEqual(prefs.sort, "closest")

    def test_half_a_home_location_is_rejected(self):
        self.assertEqual(self._patch({"home_latitude": 38.22}).status_code, 400)

    def test_out_of_range_coordinates_are_rejected(self):
        self.assertEqual(self._patch({"home_latitude": 120, "home_longitude": 0}).status_code, 400)

    def test_clearing_home_clears_its_label(self):
        self._patch({"home_label": "Louisville", "home_latitude": 38.2, "home_longitude": -85.7})
        response = self._patch({"home_latitude": None, "home_longitude": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["home_label"], "")
        self.assertIsNone(JobFilterPreferences.objects.get(owner=self.user).home)

    def test_unknown_sort_is_rejected(self):
        self.assertEqual(self._patch({"sort": "vibes"}).status_code, 400)
