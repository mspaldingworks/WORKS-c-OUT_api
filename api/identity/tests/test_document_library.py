"""
Where documents are kept (WORKS(c)OUT, Google Drive, or both), the private
download that replaced public /media/ links, and the cover-letter library.
"""

import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import DocumentSettings, GoogleDriveConnection, ResumeVersion
from ingestion.models import IngestedPosting
from tracker.drive import DriveUnavailable
from tracker.models import Application, Company

LETTER_TEXT = "I build donor programs that last, and I would bring that to your team."


def make_user(name="lib@example.test"):
    user = get_user_model().objects.create_user(name, email=name, password="pw-123456789")
    return user, {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}


def pdf(text=LETTER_TEXT):
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 720, text)
    page.save()
    return buffer.getvalue()


def connect_drive(user):
    connection = GoogleDriveConnection(owner=user, folder_id="folder-1")
    connection.set_token("refresh")
    connection.save()
    return connection


class UploadTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user()

    def _upload(self, kind="resume", name="Jane Doe Resume.pdf", text=LETTER_TEXT):
        return self.client.post(
            reverse("resumeversion-list"),
            data={"title": "Mine", "kind": kind, "file": SimpleUploadedFile(name, pdf(text))},
            **self.auth,
        )

    def test_files_are_never_handed_out_as_public_media_links(self):
        body = self._upload().json()
        self.assertNotIn("/media/", body["file"])
        self.assertIn("/api/identity/resumes/", body["file"])

    def test_stored_names_are_random_not_the_person_s_own(self):
        self._upload(name="Jane Doe Resume.pdf")
        stored = ResumeVersion.objects.get(owner=self.user).file.name
        self.assertNotIn("Jane", stored)
        self.assertTrue(stored.startswith("identity/documents/") and stored.endswith(".pdf"))

    def test_download_needs_the_owner(self):
        resume_id = self._upload().json()["id"]
        url = reverse("resumeversion-download", args=[resume_id])
        self.assertEqual(self.client.get(url, **self.auth).status_code, 200)
        _, stranger = make_user("stranger@example.test")
        self.assertEqual(self.client.get(url, **stranger).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_without_drive_everything_stays_in_the_app(self):
        body = self._upload().json()
        self.assertEqual(body["stored_in"], "app")

    def test_an_uploaded_cover_letter_keeps_a_preview_for_the_library(self):
        self._upload(kind="cover_letter")
        letter = ResumeVersion.objects.get(owner=self.user)
        self.assertIn("donor programs", letter.parsed_data["preview"])

    @patch("tracker.drive.upload_file", return_value={"id": "d-1", "webViewLink": "https://drive.test/d-1"})
    @patch("tracker.drive.credentials_for", return_value=object())
    def test_both_keeps_a_copy_here_and_in_drive(self, _creds, upload):
        connect_drive(self.user)
        body = self._upload().json()
        self.assertEqual(body["stored_in"], "both")
        self.assertEqual(body["drive_url"], "https://drive.test/d-1")
        upload.assert_called_once()

    @patch("tracker.drive.upload_file", return_value={"id": "d-1", "webViewLink": "https://drive.test/d-1"})
    @patch("tracker.drive.credentials_for", return_value=object())
    def test_drive_only_keeps_no_copy_here(self, _creds, _upload):
        connect_drive(self.user)
        DocumentSettings.objects.create(owner=self.user, storage="drive")
        body = self._upload().json()
        self.assertEqual(body["stored_in"], "drive")
        self.assertEqual(body["file"], "")
        self.assertFalse(ResumeVersion.objects.get(owner=self.user).file)
        response = self.client.get(reverse("resumeversion-download", args=[body["id"]]), **self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["drive_url"], "https://drive.test/d-1")

    @patch("tracker.drive.upload_file", side_effect=DriveUnavailable("Drive is down"))
    @patch("tracker.drive.credentials_for", return_value=object())
    def test_drive_only_keeps_the_copy_here_when_drive_fails(self, _creds, _upload):
        connect_drive(self.user)
        DocumentSettings.objects.create(owner=self.user, storage="drive")
        self.assertEqual(self._upload().json()["stored_in"], "app")

    @patch("tracker.drive.upload_file")
    def test_app_only_never_touches_drive(self, upload):
        connect_drive(self.user)
        DocumentSettings.objects.create(owner=self.user, storage="app")
        self.assertEqual(self._upload().json()["stored_in"], "app")
        upload.assert_not_called()

    @override_settings(ANTHROPIC_API_KEY="k")
    @patch("tracker.drive.download_document", return_value=("cv.pdf", pdf("Ten years of grant writing for nonprofits.")))
    @patch("tracker.drive.credentials_for", return_value=object())
    def test_a_drive_only_resume_is_read_from_drive(self, _creds, download):
        connect_drive(self.user)
        resume = ResumeVersion.objects.create(owner=self.user, title="CV", drive_file_id="d-9")
        parsed = {"skills": [], "headline": "", "email": "", "phone": "", "linkedin_url": "", "unparsed": False}
        with patch("identity.views.parse_resume_text", return_value=parsed) as parse:
            response = self.client.post(reverse("resumeversion-parse", args=[resume.pk]), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("grant writing", parse.call_args.args[0])
        download.assert_called_once()


class SettingsTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user()

    def _set(self, storage):
        return self.client.patch(reverse("document-settings"), data={"storage": storage},
                                 content_type="application/json", **self.auth)

    def test_default_is_both(self):
        body = self.client.get(reverse("document-settings"), **self.auth).json()
        self.assertEqual(body["storage"], "both")
        self.assertFalse(body["drive_available"])

    def test_drive_needs_a_connected_drive(self):
        self.assertEqual(self._set("drive").status_code, 400)
        connect_drive(self.user)
        self.assertEqual(self._set("drive").json()["storage"], "drive")

    def test_app_only_needs_nothing(self):
        self.assertEqual(self._set("app").json()["storage"], "app")

    @patch("tracker.drive.credentials_for", return_value=object())
    def test_app_only_stops_generated_pdfs_reaching_drive(self, _creds):
        from tracker.drive import resolve_drive

        connect_drive(self.user)
        self.assertIsNotNone(resolve_drive(self.user))
        self._set("app")
        self.assertIsNone(resolve_drive(self.user))


MATERIALS = {
    "cover_letter": LETTER_TEXT, "resume_summary": "Summary.", "resume_bullets": ["- Did things"],
    "gaps": [], "generated_at": "2026-10-20T12:00:00+00:00",
}


class GeneratedPdfTests(TestCase):
    def setUp(self):
        self.user, _ = make_user()
        posting = IngestedPosting.objects.create(owner=self.user, source="s", title="Grant Writer",
                                                 url="https://jobs.test/1", generated_materials=MATERIALS)
        self.application = Application.objects.create(
            owner=self.user, company=Company.objects.create(name="Helping Hands"),
            role_title="Grant Writer", source_posting=posting,
        )

    def _build(self, url):
        from ingestion.documents import build_documents

        with patch("tracker.drive.upload_pdf_quietly", return_value=url):
            build_documents(self.application)
        self.application.refresh_from_db()

    def test_drive_only_drops_the_copies_here_once_drive_has_them(self):
        DocumentSettings.objects.create(owner=self.user, storage="drive")
        self._build("https://drive.test/pdf")
        self.assertFalse(self.application.cover_letter)
        self.assertEqual(self.application.cover_letter_drive_url, "https://drive.test/pdf")

    def test_drive_only_keeps_the_copies_here_if_drive_failed(self):
        DocumentSettings.objects.create(owner=self.user, storage="drive")
        self._build("")
        self.assertTrue(self.application.cover_letter)

    def test_both_keeps_both(self):
        self._build("https://drive.test/pdf")
        self.assertTrue(self.application.cover_letter)

    def test_generation_records_when_a_letter_was_written(self):
        from unittest.mock import MagicMock

        from identity import llm
        from ingestion.generation import generate_materials

        posting = MagicMock(pk=1, title="T", company_name="C")
        with patch("ingestion.generation.payload_of", return_value={"descriptionText": "x" * 300}), \
             patch("identity.llm.complete", return_value="letter"):
            materials = generate_materials(posting, "History.", config=llm.LLMConfig("anthropic", "k", "m", ""))
        self.assertIn("generated_at", materials)


class LibraryTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user()
        self.applied = IngestedPosting.objects.create(
            owner=self.user, source="s", title="Grant Writer", company_name="Zeta Org",
            url="https://jobs.test/a", generated_materials={**MATERIALS, "generated_at": "2026-10-01T12:00:00+00:00"},
        )
        Application.objects.create(owner=self.user, company=Company.objects.create(name="Zeta Org"),
                                   role_title="Grant Writer", source_posting=self.applied,
                                   status=Application.Status.APPLIED)
        IngestedPosting.objects.create(
            owner=self.user, source="s", title="Annual Fund Manager", company_name="Alpha Fund",
            url="https://jobs.test/b", generated_materials={**MATERIALS, "generated_at": "2026-10-10T12:00:00+00:00"},
        )
        IngestedPosting.objects.create(owner=self.user, source="s", title="No letter", url="https://jobs.test/c")
        self.client.post(reverse("resumeversion-list"),
                         data={"title": "My own letter", "kind": "cover_letter",
                               "file": SimpleUploadedFile("l.pdf", pdf())}, **self.auth)

    def _get(self, query=""):
        response = self.client.get(reverse("cover-letter-library") + query, **self.auth)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_written_and_uploaded_letters_are_together_newest_first(self):
        rows = self._get()
        self.assertEqual([row["title"] for row in rows], ["My own letter", "Annual Fund Manager", "Grant Writer"])

    def test_written_letters_carry_their_text_and_where_they_stand(self):
        rows = {row["title"]: row for row in self._get("?source=generated")}
        self.assertEqual(set(rows), {"Annual Fund Manager", "Grant Writer"})
        self.assertEqual(rows["Grant Writer"]["text"], LETTER_TEXT)
        self.assertEqual(rows["Grant Writer"]["status"], "Applied")
        self.assertEqual(rows["Annual Fund Manager"]["status"], "Not applied yet")

    def test_uploaded_letters_show_a_preview(self):
        rows = self._get("?source=uploaded")
        self.assertEqual(len(rows), 1)
        self.assertIn("donor programs", rows[0]["preview"])
        self.assertIn("/download/", rows[0]["file_url"])

    def test_sorts(self):
        self.assertEqual([r["title"] for r in self._get("?sort=oldest")][0], "Grant Writer")
        self.assertEqual([r["company"] for r in self._get("?sort=company")][:2], ["Alpha Fund", "Zeta Org"])
        self.assertEqual([r["title"] for r in self._get("?sort=role")][0], "Annual Fund Manager")

    def test_another_account_sees_none_of_them(self):
        _, stranger = make_user("stranger@example.test")
        self.assertEqual(self.client.get(reverse("cover-letter-library"), **stranger).json(), [])

    def test_unknown_sort_is_refused(self):
        response = self.client.get(reverse("cover-letter-library") + "?sort=size", **self.auth)
        self.assertEqual(response.status_code, 400)
