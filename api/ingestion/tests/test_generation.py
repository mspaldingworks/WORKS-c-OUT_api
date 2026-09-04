from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ProfessionalProfile
from ingestion.generation import GenerationUnavailable, generate_materials
from ingestion.models import IngestedPosting

WELL_FORMED = """===COVER_LETTER===
Dear Hiring Team,

I lead development at a youth nonprofit and grew our giving day from $11,950 to $17,000.

Madelyn
===RESUME_SUMMARY===
Development professional with five years of nonprofit fundraising experience.
===RESUME_BULLETS===
- Grew Give for Good Louisville to $17,000 from 117 donors
- Migrated donor data from Salesforce to Little Green Light
===GAPS===
- Salesforce Marketing Cloud: you have Salesforce as a donor CRM, not Marketing Cloud.
- PMP certification preferred: she doesn't hold one.
"""


def fake_response(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def stub_stream(client, text):
    """
    Wire a mocked Anthropic client so messages.stream(...) yields `text`.

    The real call streams (a 16k-token response with thinking would otherwise
    hit request timeouts), so the mock has to match that shape: stream() returns
    a context manager whose get_final_message() gives the finished message.
    """
    stream = client.return_value.messages.stream
    stream.return_value.__enter__.return_value.get_final_message.return_value = fake_response(text)
    return stream


def make_posting(description="x" * 500, url="https://example.test/job/1"):
    return IngestedPosting.objects.create(
        source="apify:indeed",
        title="Digital Fundraising Strategy Lead",
        company_name="American Heart Association",
        url=url,
        raw_payload={"descriptionText": description, "salary": {}, "location": {}},
    )


@override_settings(ANTHROPIC_API_KEY="test-key")
class GenerateMaterialsTests(TestCase):
    def test_parses_the_four_sections(self):
        posting = make_posting()
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            materials = generate_materials(posting, "Madelyn Spalding — Director of Development...")

        self.assertIn("$17,000", materials["cover_letter"])
        self.assertIn("Development professional", materials["resume_summary"])
        self.assertEqual(len(materials["resume_bullets"]), 2)
        self.assertTrue(materials["resume_bullets"][0].startswith("Grew Give for Good"))
        self.assertEqual(len(materials["gaps"]), 2)
        # Markers are stripped so the UI styles its own list, and a stray "-"
        # doesn't show up in front of every gap.
        self.assertTrue(materials["gaps"][0].startswith("Salesforce Marketing Cloud"))
        self.assertTrue(materials["resume_bullets"][0].startswith("Grew Give for Good"))
        self.assertFalse(materials["unparsed"])

    def test_keeps_the_output_when_the_format_markers_are_missing(self):
        # A formatting miss shouldn't throw away work that costs money to produce.
        posting = make_posting()
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, "Dear Hiring Team, ...")
            materials = generate_materials(posting, "background")

        self.assertTrue(materials["unparsed"])
        self.assertIn("Dear Hiring Team", materials["cover_letter"])

    def test_uses_opus_5_and_sends_both_the_background_and_the_posting(self):
        posting = make_posting(description="We need a fundraiser. " * 40)
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            generate_materials(posting, "CANDIDATE BACKGROUND TEXT")

        kwargs = client.return_value.messages.stream.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5")
        sent = kwargs["messages"][0]["content"]
        self.assertIn("CANDIDATE BACKGROUND TEXT", sent)
        self.assertIn("We need a fundraiser", sent)
        self.assertIn("never invent", kwargs["system"].lower().replace("  ", " "))

    def test_the_letter_is_told_not_to_concede_gaps(self):
        # Gaps belong in GAPS, which only she sees. A letter that argues against
        # itself was the first version's actual behaviour, so guard it.
        posting = make_posting(url="https://example.test/job/9")
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            generate_materials(posting, "background")

        system = client.return_value.messages.stream.call_args.kwargs["system"].lower()
        self.assertIn("never concede", system)
        self.assertIn("shortcomings belong in gaps", system)

    def test_refuses_clearly_when_prerequisites_are_missing(self):
        posting = make_posting()
        with self.assertRaises(GenerationUnavailable):
            generate_materials(posting, "   ")  # no master resume

        with override_settings(ANTHROPIC_API_KEY=""):
            with self.assertRaises(GenerationUnavailable):
                generate_materials(posting, "background")

        thin = make_posting(description="too short", url="https://example.test/job/2")
        with self.assertRaises(GenerationUnavailable):
            generate_materials(thin, "background")

    def test_api_failure_becomes_a_readable_error(self):
        posting = make_posting()
        with patch("anthropic.Anthropic") as client:
            client.return_value.messages.stream.side_effect = RuntimeError("overloaded")
            with self.assertRaises(GenerationUnavailable):
                generate_materials(posting, "background")


@override_settings(ANTHROPIC_API_KEY="test-key")
class MaterialsEndpointTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}
        self.posting = make_posting()
        ProfessionalProfile.objects.create(headline="Director", master_resume="Her real background.")

    def url(self, refresh=False):
        base = reverse("ingestedposting-materials", args=[self.posting.pk])
        return base + ("?refresh=1" if refresh else "")

    def test_generates_then_serves_from_cache(self):
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            first = self.client.post(self.url(), **self.auth)
            second = self.client.post(self.url(), **self.auth)
            # Second call must not cost money again.
            self.assertEqual(client.return_value.messages.stream.call_count, 1)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["cover_letter"], second.json()["cover_letter"])
        self.posting.refresh_from_db()
        self.assertTrue(self.posting.generated_materials)

    def test_refresh_regenerates(self):
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            self.client.post(self.url(), **self.auth)
            self.client.post(self.url(refresh=True), **self.auth)
            self.assertEqual(client.return_value.messages.stream.call_count, 2)

    def test_missing_key_returns_503_with_a_usable_message(self):
        with override_settings(ANTHROPIC_API_KEY=""):
            response = self.client.post(self.url(), **self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertIn("Anthropic API key", response.json()["detail"])

    def test_requires_authentication(self):
        self.assertEqual(self.client.post(self.url()).status_code, 401)
