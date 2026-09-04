"""
Résumé text extraction and AI parsing.

The Anthropic mocking pattern mirrors ingestion/tests/test_generation.py:
messages.stream(...) is a context manager whose get_final_message() returns
the finished response — the real call streams because thinking plus a
multi-thousand-token response can otherwise trip a request timeout.
"""

import io
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ResumeVersion
from identity.resume_parsing import ParsingUnavailable, parse_resume_text
from identity.resume_text import TextExtractionFailed, extract_resume_text

WELL_FORMED = """{
  "skills": [
    {"name": "Python", "category": "Software", "proficiency": "strong"},
    {"name": "Fundraising", "category": "Development", "proficiency": "expert"}
  ],
  "headline": "Director of Development",
  "email": "madelyn@example.test",
  "phone": "502-555-0100",
  "linkedin_url": "https://linkedin.test/madelyn"
}"""

MEDIA = tempfile.mkdtemp()
ISOLATED_STORAGE = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": MEDIA}},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def fake_response(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def stub_stream(client, text):
    stream = client.return_value.messages.stream
    stream.return_value.__enter__.return_value.get_final_message.return_value = fake_response(text)
    return stream


def pdf_with_text(text):
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, text)
    pdf.save()
    return buffer.getvalue()


def docx_with_text(text):
    from docx import Document

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


@override_settings(ANTHROPIC_API_KEY="test-key")
class ParseResumeTextTests(TestCase):
    def test_parses_well_formed_json(self):
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            result = parse_resume_text("Madelyn Spalding — Director of Development. Python, fundraising.")

        self.assertEqual(len(result["skills"]), 2)
        self.assertEqual(result["skills"][0]["name"], "Python")
        self.assertEqual(result["skills"][0]["proficiency"], "strong")
        self.assertEqual(result["headline"], "Director of Development")
        self.assertFalse(result["unparsed"])

    def test_strips_a_code_fence_around_the_json(self):
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, f"```json\n{WELL_FORMED}\n```")
            result = parse_resume_text("some resume text " * 5)

        self.assertEqual(len(result["skills"]), 2)
        self.assertFalse(result["unparsed"])

    def test_keeps_the_raw_text_when_the_response_is_not_json(self):
        # Don't throw away a paid-for generation over a formatting miss.
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, "I couldn't find a clear resume structure here.")
            result = parse_resume_text("some resume text " * 5)

        self.assertTrue(result["unparsed"])
        self.assertEqual(result["skills"], [])

    def test_an_invalid_proficiency_falls_back_to_competent(self):
        malformed = WELL_FORMED.replace('"strong"', '"legendary"')
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, malformed)
            result = parse_resume_text("some resume text " * 5)

        self.assertEqual(result["skills"][0]["proficiency"], "competent")

    def test_refuses_clearly_when_prerequisites_are_missing(self):
        with override_settings(ANTHROPIC_API_KEY=""):
            with self.assertRaises(ParsingUnavailable):
                parse_resume_text("plenty of resume text here " * 5)

        with self.assertRaises(ParsingUnavailable):
            parse_resume_text("too short")

    def test_api_failure_becomes_a_readable_error(self):
        with patch("anthropic.Anthropic") as client:
            client.return_value.messages.stream.side_effect = RuntimeError("overloaded")
            with self.assertRaises(ParsingUnavailable):
                parse_resume_text("plenty of resume text here " * 5)


class ExtractResumeTextTests(TestCase):
    def test_extracts_text_from_a_real_pdf(self):
        upload_file = SimpleUploadedFile("r.pdf", pdf_with_text("Madelyn Spalding, Director of Development"))
        text = extract_resume_text(upload_file)
        self.assertIn("Madelyn Spalding", text)

    def test_extracts_text_from_a_real_docx(self):
        upload_file = SimpleUploadedFile("r.docx", docx_with_text("Madelyn Spalding, Director of Development"))
        text = extract_resume_text(upload_file)
        self.assertIn("Madelyn Spalding", text)

    def test_a_blank_pdf_raises_a_readable_error(self):
        from reportlab.pdfgen import canvas

        buffer = io.BytesIO()
        canvas.Canvas(buffer).save()  # no text drawn at all
        upload_file = SimpleUploadedFile("blank.pdf", buffer.getvalue())
        with self.assertRaises(TextExtractionFailed):
            extract_resume_text(upload_file)

    def test_a_corrupt_file_raises_a_readable_error(self):
        upload_file = SimpleUploadedFile("broken.pdf", b"not a pdf at all")
        with self.assertRaises(TextExtractionFailed):
            extract_resume_text(upload_file)


@override_settings(ANTHROPIC_API_KEY="test-key", MEDIA_ROOT=MEDIA, STORAGES=ISOLATED_STORAGE)
class ParseEndpointTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=self.user).key}"}
        self.resume = ResumeVersion.objects.create(
            owner=self.user, title="My résumé",
            file=SimpleUploadedFile("r.pdf", pdf_with_text("Madelyn Spalding, Director of Development. Python.")),
        )

    def url(self, refresh=False):
        base = reverse("resumeversion-parse", args=[self.resume.pk])
        return base + ("?refresh=1" if refresh else "")

    def test_parses_then_serves_from_cache(self):
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            first = self.client.post(self.url(), **self.auth)
            second = self.client.post(self.url(), **self.auth)
            self.assertEqual(client.return_value.messages.stream.call_count, 1)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["skills"][0]["name"], "Python")
        self.assertEqual(second.json(), first.json())
        self.resume.refresh_from_db()
        self.assertTrue(self.resume.parsed_data)

    def test_refresh_reparses(self):
        with patch("anthropic.Anthropic") as client:
            stub_stream(client, WELL_FORMED)
            self.client.post(self.url(), **self.auth)
            self.client.post(self.url(refresh=True), **self.auth)
            self.assertEqual(client.return_value.messages.stream.call_count, 2)

    def test_missing_key_returns_503(self):
        with override_settings(ANTHROPIC_API_KEY=""):
            response = self.client.post(self.url(), **self.auth)
        self.assertEqual(response.status_code, 503)

    def test_another_account_cannot_parse_someone_else_s_resume(self):
        other = get_user_model().objects.create_user("other", password="x")
        other_auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=other).key}"}
        response = self.client.post(self.url(), **other_auth)
        self.assertEqual(response.status_code, 404)

    def test_requires_authentication(self):
        self.assertEqual(self.client.post(self.url()).status_code, 401)
