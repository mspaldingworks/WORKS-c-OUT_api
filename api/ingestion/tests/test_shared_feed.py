"""
The shared posting feed.

Why it exists: TransWell used to mirror postings by authenticating as the
owning account, so a partner app held a person's own long-lived credential and
could reach their résumés, profile and applications. These tests pin the two
properties that make this endpoint a replacement rather than a rename — it
needs no user token, and it serves postings without the fields that describe
one person's job search.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from ingestion.feed_views import PERSONAL_FIELDS
from ingestion.models import IngestedPosting

KEY = 'feed-key-for-tests'
OWNER_EMAIL = 'owner@example.test'


@override_settings(FEED_API_KEY=KEY, WORKS_COUT_OWNER_EMAIL=OWNER_EMAIL)
class SharedPostingFeedTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='feedowner', email=OWNER_EMAIL, password='x',
        )
        self.posting = IngestedPosting.objects.create(
            owner=self.owner,
            source='apify:indeed',
            title='Peer Support Specialist',
            company_name='Affirming Health',
            url='https://example.test/jobs/1',
            status='triaged',
            score=100,
            score_reasons=['Matches your communications lane'],
        )
        self.url = reverse('shared-posting-feed')

    def get(self, key=KEY):
        return self.client.get(self.url, HTTP_X_FEED_KEY=key)

    def test_the_key_alone_reads_the_feed(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['title'], 'Peer Support Specialist')

    def test_no_user_token_is_needed(self):
        # The point of the endpoint: a partner app should not have to hold
        # somebody's personal credential to mirror a job board.
        self.assertNotIn('HTTP_AUTHORIZATION', self.client.defaults)
        self.assertEqual(self.get().status_code, 200)

    def test_a_wrong_or_missing_key_reads_nothing(self):
        for key in ('', 'not-the-key'):
            with self.subTest(key=key):
                self.assertEqual(self.get(key=key).status_code, 401)

    @override_settings(FEED_API_KEY='')
    def test_an_unset_key_refuses_rather_than_accepting_anything(self):
        self.assertEqual(self.get(key='').status_code, 401)

    def test_personal_fields_never_cross_the_wire(self):
        # TransWell drops these on arrival too, but stripping here is the
        # difference between choosing not to look and not being shown.
        row = self.get().json()['results'][0]

        for field in PERSONAL_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, row)
        self.assertNotIn('Matches your communications lane', str(row))
        self.assertNotIn('triaged', str(row))

    def test_the_job_itself_still_comes_through(self):
        row = self.get().json()['results'][0]

        for field in ('id', 'title', 'company_name', 'url', 'details', 'skills'):
            with self.subTest(field=field):
                self.assertIn(field, row)

    def test_another_account_s_postings_are_not_served(self):
        User = get_user_model()
        other = User.objects.create_user(
            username='someone-else', email='other@example.test', password='x',
        )
        IngestedPosting.objects.create(
            owner=other, source='apify:indeed', title='Not in the feed',
            company_name='Elsewhere', url='https://example.test/jobs/2',
        )

        titles = [r['title'] for r in self.get().json()['results']]

        self.assertNotIn('Not in the feed', titles)

    def test_it_is_read_only(self):
        response = self.client.post(
            self.url, {'title': 'Injected'}, HTTP_X_FEED_KEY=KEY,
        )

        self.assertEqual(response.status_code, 405)
        self.assertEqual(IngestedPosting.objects.count(), 1)

    def test_the_feed_key_cannot_read_anything_else(self):
        # A leaked feed key must not become a way into the identity data.
        for path in ('/api/identity/resumes/', '/api/identity/profile/'):
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_X_FEED_KEY=KEY)
                self.assertEqual(response.status_code, 401)

    def test_a_user_token_still_cannot_see_another_account_s_postings(self):
        # The old viewset stays owner-scoped; this endpoint doesn't loosen it.
        User = get_user_model()
        stranger = User.objects.create_user(
            username='stranger', email='stranger@example.test', password='x',
        )
        token, _ = Token.objects.get_or_create(user=stranger)

        response = self.client.get(
            '/api/ingestion/postings/',
            HTTP_AUTHORIZATION=f'Token {token.key}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
