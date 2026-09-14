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
        self.assertTrue(body["salary"])       # salary on by default
        self.assertFalse(body["remote"])
        self.assertFalse(body["job_type"])
        self.assertFalse(body["match_score"])
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
        JobFilterPreferences.objects.create(owner=other, remote=True)
        # My row is created fresh with defaults, not read from someone else's.
        self.assertFalse(self.client.get(self.url, **self._auth()).json()["remote"])
