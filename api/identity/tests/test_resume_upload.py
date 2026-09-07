"""
Résumé uploads are validated server-side — a client-side check in the Swift
app's file picker is not a limit, since anyone can call the API directly.
These hit the real endpoint (not just validate_resume_file directly) so a
regression in how the serializer wires the validator in would actually fail.
"""

import io
import shutil
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ResumeVersion
from identity.validators import MAX_RESUME_BYTES, MAX_RESUMES_PER_OWNER

MEDIA = tempfile.mkdtemp()
# Overriding MEDIA_ROOT alone doesn't redirect writes — default storage is
# built once and cached (see tracker/tests/test_documents.py for the same
# note) — so STORAGES has to be pinned too, or the suite writes real files.
ISOLATED_STORAGE = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": MEDIA}},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def real_pdf_bytes(extra=b""):
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + extra + b"\n%%EOF"


def real_docx_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA, STORAGES=ISOLATED_STORAGE)
class ResumeUploadTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=self.user).key}"}

    def upload(self, upload_file, title="My résumé"):
        return self.client.post(
            reverse("resumeversion-list"), {"title": title, "file": upload_file}, **self.auth
        )

    def test_accepts_a_real_pdf(self):
        upload_file = SimpleUploadedFile("resume.pdf", real_pdf_bytes(), content_type="application/pdf")
        response = self.upload(upload_file)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(ResumeVersion.objects.get().owner, self.user)

    def test_accepts_a_real_docx(self):
        upload_file = SimpleUploadedFile(
            "resume.docx", real_docx_bytes(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response = self.upload(upload_file)
        self.assertEqual(response.status_code, 201, response.content)

    def test_rejects_a_disallowed_extension(self):
        upload_file = SimpleUploadedFile("resume.exe", b"MZ" + b"x" * 100, content_type="application/octet-stream")
        response = self.upload(upload_file)
        self.assertEqual(response.status_code, 400)
        self.assertIn("PDF, DOCX, TXT, Markdown and RTF", str(response.json()))
        self.assertEqual(ResumeVersion.objects.count(), 0)

    def test_accepts_the_plain_text_formats_the_parser_can_read(self):
        # Storage and reading share one list (see validators). A format the
        # parser handles must not be refused at upload — that mismatch is what
        # made TransWell's .txt uploads fail after they had already been saved.
        for name, data in (
            ("resume.txt", b"Peer support and intake experience"),
            ("resume.md", b"# Resume\n\nPeer support"),
            (b"resume.rtf".decode(), rb"{\rtf1\ansi Peer support}"),
        ):
            with self.subTest(name=name):
                response = self.upload(SimpleUploadedFile(name, data))
                self.assertEqual(response.status_code, 201, response.json())

    def test_a_renamed_file_with_a_pdf_extension_is_still_rejected(self):
        # The extension alone is exactly what a client-side check would trust.
        upload_file = SimpleUploadedFile("resume.pdf", b"actually just some text, not a pdf",
                                         content_type="application/pdf")
        response = self.upload(upload_file)
        self.assertEqual(response.status_code, 400)
        self.assertIn("real PDF", str(response.json()))

    def test_a_renamed_file_with_a_docx_extension_is_still_rejected(self):
        upload_file = SimpleUploadedFile("resume.docx", b"not a zip at all", content_type="application/octet-stream")
        response = self.upload(upload_file)
        self.assertEqual(response.status_code, 400)
        self.assertIn("real DOCX", str(response.json()))

    def test_rejects_a_file_over_the_size_cap(self):
        oversized = real_pdf_bytes(b"x" * (MAX_RESUME_BYTES + 1))
        upload_file = SimpleUploadedFile("resume.pdf", oversized, content_type="application/pdf")
        response = self.upload(upload_file)
        self.assertEqual(response.status_code, 400)
        self.assertIn("10MB", str(response.json()))

    def test_enforces_a_per_account_count_limit(self):
        for i in range(MAX_RESUMES_PER_OWNER):
            upload_file = SimpleUploadedFile(f"resume{i}.pdf", real_pdf_bytes(), content_type="application/pdf")
            self.assertEqual(self.upload(upload_file, title=f"v{i}").status_code, 201)

        one_more = SimpleUploadedFile("one_more.pdf", real_pdf_bytes(), content_type="application/pdf")
        response = self.upload(one_more, title="one too many")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ResumeVersion.objects.count(), MAX_RESUMES_PER_OWNER)

    def test_the_count_limit_is_per_account_not_global(self):
        other = get_user_model().objects.create_user("other", password="x")
        other_auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=other).key}"}
        for i in range(MAX_RESUMES_PER_OWNER):
            ResumeVersion.objects.create(
                owner=self.user, title=f"v{i}",
                file=SimpleUploadedFile(f"r{i}.pdf", real_pdf_bytes()),
            )

        upload_file = SimpleUploadedFile("resume.pdf", real_pdf_bytes(), content_type="application/pdf")
        response = self.client.post(
            reverse("resumeversion-list"), {"title": "First for this account", "file": upload_file}, **other_auth
        )
        self.assertEqual(response.status_code, 201)

    def test_requires_authentication(self):
        upload_file = SimpleUploadedFile("resume.pdf", real_pdf_bytes(), content_type="application/pdf")
        response = self.client.post(reverse("resumeversion-list"), {"title": "x", "file": upload_file})
        self.assertEqual(response.status_code, 401)


@override_settings(MEDIA_ROOT=MEDIA, STORAGES=ISOLATED_STORAGE)
class ResumeRemovalTests(TestCase):
    """
    Removing a résumé is reversible. CLAUDE.md §3.5: confirmation dialogs get
    dismissed reflexively, so they protect nothing — undo actually does.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=self.user).key}"}
        self.resume = ResumeVersion.objects.create(
            owner=self.user, title="Mine", file=SimpleUploadedFile("r.pdf", real_pdf_bytes()),
            parsed_data={"skills": [{"name": "Python", "category": "", "proficiency": "strong"}]},
        )

    def test_discarding_hides_it_without_destroying_it(self):
        response = self.client.post(reverse("resumeversion-discard", args=[self.resume.pk]), **self.auth)
        self.assertEqual(response.status_code, 200)

        listed = self.client.get(reverse("resumeversion-list"), **self.auth).json()
        self.assertEqual(listed, [])
        # The row survives, so undo has something to put back.
        self.resume.refresh_from_db()
        self.assertIsNotNone(self.resume.discarded_at)

    def test_restore_puts_it_back_with_its_suggestions(self):
        self.client.post(reverse("resumeversion-discard", args=[self.resume.pk]), **self.auth)
        response = self.client.post(reverse("resumeversion-restore", args=[self.resume.pk]), **self.auth)

        self.assertEqual(response.status_code, 200)
        listed = self.client.get(reverse("resumeversion-list"), **self.auth).json()
        self.assertEqual(len(listed), 1)
        # Parsing cost a real model call; removing and undoing must not burn it.
        self.assertEqual(listed[0]["parsed_data"]["skills"][0]["name"], "Python")

    def test_discarding_frees_a_slot_against_the_count_cap(self):
        for i in range(MAX_RESUMES_PER_OWNER - 1):
            ResumeVersion.objects.create(
                owner=self.user, title=f"v{i}", file=SimpleUploadedFile(f"r{i}.pdf", real_pdf_bytes())
            )
        at_cap = SimpleUploadedFile("over.pdf", real_pdf_bytes(), content_type="application/pdf")
        self.assertEqual(
            self.client.post(reverse("resumeversion-list"),
                             {"title": "over", "file": at_cap}, **self.auth).status_code,
            400,
        )

        self.client.post(reverse("resumeversion-discard", args=[self.resume.pk]), **self.auth)

        now_fits = SimpleUploadedFile("fits.pdf", real_pdf_bytes(), content_type="application/pdf")
        self.assertEqual(
            self.client.post(reverse("resumeversion-list"),
                             {"title": "fits", "file": now_fits}, **self.auth).status_code,
            201,
        )

    def test_another_account_cannot_discard_or_restore_it(self):
        other = get_user_model().objects.create_user("other", password="x")
        other_auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=other).key}"}
        for name in ("resumeversion-discard", "resumeversion-restore"):
            with self.subTest(name=name):
                response = self.client.post(reverse(name, args=[self.resume.pk]), **other_auth)
                self.assertEqual(response.status_code, 404)
        self.resume.refresh_from_db()
        self.assertIsNone(self.resume.discarded_at)

    def test_a_discarded_resume_cannot_be_parsed(self):
        self.client.post(reverse("resumeversion-discard", args=[self.resume.pk]), **self.auth)
        response = self.client.post(reverse("resumeversion-parse", args=[self.resume.pk]), **self.auth)
        self.assertEqual(response.status_code, 404)
