import io
import urllib.error
from unittest.mock import MagicMock

from django.test import TestCase

from ingestion.freshness import check_url, sweep
from ingestion.models import IngestedPosting


def response_with(body="a normal job description", status=200):
    """A stand-in for urlopen's context manager."""
    handle = MagicMock()
    handle.read.return_value = body.encode()
    handle.status = status
    handle.__enter__ = lambda self: self
    handle.__exit__ = lambda self, *args: False
    return lambda request, timeout=None: handle


def raising(error):
    def opener(request, timeout=None):
        raise error
    return opener


class CheckUrlTests(TestCase):
    def test_a_live_page_is_live(self):
        verdict, _ = check_url("https://example.test/job/1", opener=response_with())
        self.assertEqual(verdict, "live")

    def test_404_and_410_are_gone(self):
        for code in (404, 410):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("u", code, "gone", {}, io.BytesIO(b""))
                verdict, detail = check_url("https://example.test/job/1", opener=raising(error))
                self.assertEqual(verdict, "gone")
                self.assertIn(str(code), detail)

    def test_403_is_not_evidence_of_removal(self):
        # Several ATS return 403 to anything that looks scripted. Retiring a
        # live posting is much worse than leaving a dead one up another day.
        error = urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b""))
        verdict, _ = check_url("https://example.test/job/1", opener=raising(error))
        self.assertEqual(verdict, "unknown")

    def test_a_200_page_that_says_it_is_closed_counts_as_gone(self):
        # The common case: the ATS serves a friendly "this role is filled" page
        # with a perfectly successful status code.
        for phrase in ("This job is no longer accepting applications.",
                       "The position has been filled.",
                       "Sorry, this posting has expired."):
            with self.subTest(phrase=phrase):
                verdict, detail = check_url(
                    "https://example.test/job/1", opener=response_with(phrase))
                self.assertEqual(verdict, "gone")
                self.assertIn("says", detail)

    def test_ordinary_wording_is_not_mistaken_for_closure(self):
        # "no longer" on its own appears in plenty of live descriptions.
        body = ("We are no longer a startup — we're a 200-person company. "
                "Applications are open until filled.")
        verdict, _ = check_url("https://example.test/job/1", opener=response_with(body))
        self.assertEqual(verdict, "live")

    def test_network_trouble_is_unknown_not_gone(self):
        verdict, _ = check_url("https://example.test/job/1", opener=raising(TimeoutError("slow")))
        self.assertEqual(verdict, "unknown")

    def test_a_posting_with_no_url_is_unknown(self):
        self.assertEqual(check_url("")[0], "unknown")


class SweepTests(TestCase):
    def make(self, url, status=IngestedPosting.Status.NEW):
        return IngestedPosting.objects.create(
            source="apify:indeed", title="Program Manager", company_name="Acme",
            url=url, status=status)

    def test_retires_only_the_dead_ones(self):
        dead = self.make("https://example.test/job/dead")
        alive = self.make("https://example.test/job/alive")

        def opener(request, timeout=None):
            if "dead" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, io.BytesIO(b""))
            return response_with()(request)

        summary = sweep([dead, alive], opener=opener)

        self.assertEqual(summary["retired"], 1)
        self.assertEqual(summary["live"], 1)
        dead.refresh_from_db()
        alive.refresh_from_db()
        self.assertEqual(dead.status, IngestedPosting.Status.EXPIRED)
        self.assertEqual(alive.status, IngestedPosting.Status.NEW)

    def test_an_unreachable_posting_is_left_alone(self):
        posting = self.make("https://example.test/job/x")
        summary = sweep([posting], opener=raising(TimeoutError("slow")))

        self.assertEqual(summary["retired"], 0)
        self.assertEqual(summary["unknown"], 1)
        posting.refresh_from_db()
        self.assertEqual(posting.status, IngestedPosting.Status.NEW)

    def test_expired_postings_leave_the_new_feed(self):
        posting = self.make("https://example.test/job/dead")
        error = urllib.error.HTTPError("u", 404, "gone", {}, io.BytesIO(b""))
        sweep([posting], opener=raising(error))

        remaining = IngestedPosting.objects.filter(status=IngestedPosting.Status.NEW)
        self.assertNotIn(posting, remaining)
