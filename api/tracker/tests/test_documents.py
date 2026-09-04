import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ProfessionalProfile
from ingestion.documents import RESUME_SECTIONS, _master_resume_sections, build_documents
from ingestion.models import IngestedPosting
from ingestion.services import promote_posting_to_application

MASTER_RESUME = """# Madelyn Spalding — master background

Louisville, KY.

## Current role

**Director of Development — Louisville Youth Group** (May 2023–present)

- Grew Give for Good Louisville from $11,950 to $17,000.

## Earlier roles

**Supervisor — Louisville Metro Parks & Recreation** (2022–2023)

## Education

- BFA, Painting — Murray State University

## Positioning notes for the generator

Five lanes she is actively targeting. NEVER SHOW THIS TO AN EMPLOYER.
"""

MATERIALS = {
    "cover_letter": "Dear Hiring Team,\n\nI grew giving from $11,950 to $17,000.\n\nMadelyn",
    "resume_summary": "Development leader with nonprofit fundraising experience.",
    "resume_bullets": ["Grew Give for Good to $17,000", "Migrated Salesforce to Little Green Light"],
    "gaps": ["No PMP."],
    "unparsed": False,
}

MEDIA = tempfile.mkdtemp()

# Overriding MEDIA_ROOT alone does NOT redirect file writes: the default storage
# is built once from STORAGES and cached, so it keeps the location it was
# constructed with. Pinning the location in STORAGES is what actually isolates
# the test — without this, running the suite writes PDFs into production media.
ISOLATED_STORAGE = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": MEDIA},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(MEDIA_ROOT=MEDIA, STORAGES=ISOLATED_STORAGE)
class DocumentRenderingTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # MEDIA is shared by every test in this class, so a file left by an
        # earlier test would collide and get a uniquified name here — which
        # looks exactly like the orphaning bug this class is checking for.
        shutil.rmtree(MEDIA, ignore_errors=True)

        ProfessionalProfile.objects.create(
            legal_name="Madelyn Spalding",
            email="mspaldingworks@gmail.com",
            phone="502-552-5981",
            city_state="Louisville, KY",
            linkedin_url="https://www.linkedin.com/in/madelynspalding",
            master_resume=MASTER_RESUME,
        )
        self.posting = IngestedPosting.objects.create(
            source="apify:indeed",
            title="Director of Development",
            company_name="American Heart Association",
            url="https://example.test/job/1",
            raw_payload={"descriptionText": "x" * 500},
            generated_materials=MATERIALS,
        )
        self.application = promote_posting_to_application(self.posting)

    def test_rendering_does_not_reach_google(self):
        # The suite runs against the production container with live credentials
        # in the environment, and this path uploads to Drive. It wrote fixture
        # PDFs into her real folder once; settings blanks the credentials under
        # test so it can't happen again.
        from django.conf import settings

        self.assertEqual(settings.JOB_DRIVE_FOLDER_ID, "")
        self.assertEqual(settings.GOOGLE_OAUTH_REFRESH_TOKEN, "")
        self.assertEqual(settings.JOB_SHEET_ID, "")

    def test_renders_both_pdfs(self):
        written = build_documents(self.application)

        self.assertEqual(len(written), 2)
        self.application.refresh_from_db()
        self.assertTrue(self.application.cover_letter.name.endswith(".pdf"))
        self.assertTrue(self.application.resume.name.endswith(".pdf"))
        # A real PDF, not an empty file.
        self.assertEqual(self.application.resume.open("rb").read(4), b"%PDF")
        self.assertGreater(self.application.resume.size, 1000)

    def test_file_names_stay_within_the_field_limit(self):
        # Over 100 chars Django appends a random suffix on every save, so the
        # file can never be replaced in place. Use the longest realistic names.
        self.application.company.name = "American Heart Association of the Commonwealth"
        self.application.company.save()
        self.application.role_title = "Senior Director of Development and Digital Fundraising Strategy"
        self.application.save(update_fields=["role_title"])

        build_documents(self.application)
        self.application.refresh_from_db()
        for name in (self.application.resume.name, self.application.cover_letter.name):
            self.assertLessEqual(len(name), 100, name)

    def test_does_nothing_without_generated_materials(self):
        self.posting.generated_materials = {}
        self.posting.save(update_fields=["generated_materials"])
        self.assertEqual(build_documents(self.application), [])

    def test_only_whitelisted_master_resume_sections_are_extracted(self):
        # The positioning notes are written for the generator, not an employer.
        # Leaking them onto a resume would be actively damaging.
        sections = dict(_master_resume_sections(MASTER_RESUME))
        self.assertIn("Current role", sections)
        self.assertIn("Education", sections)
        self.assertNotIn("Positioning notes for the generator", sections)
        for heading in sections:
            self.assertIn(heading, RESUME_SECTIONS)

    def test_rerendering_replaces_rather_than_orphaning_files(self):
        import os

        build_documents(self.application)
        build_documents(self.application)
        build_documents(self.application)
        self.application.refresh_from_db()

        # Three renders must leave one resume and one letter, not six files.
        resumes = os.listdir(os.path.join(MEDIA, "applications", "resumes"))
        letters = os.listdir(os.path.join(MEDIA, "applications", "cover_letters"))
        self.assertEqual(len(resumes), 1, resumes)
        self.assertEqual(len(letters), 1, letters)
        self.assertGreater(self.application.resume.size, 1000)

    def test_file_names_stay_within_the_field_limit(self):
        # Over 100 chars Django appends a random suffix on every save, so the
        # file can never be replaced in place. Use the longest realistic names.
        self.application.company.name = "American Heart Association of the Commonwealth"
        self.application.company.save()
        self.application.role_title = "Senior Director of Development and Digital Fundraising Strategy"
        self.application.save(update_fields=["role_title"])

        build_documents(self.application)
        self.application.refresh_from_db()
        for name in (self.application.resume.name, self.application.cover_letter.name):
            self.assertLessEqual(len(name), 100, name)


@override_settings(MEDIA_ROOT=MEDIA, STORAGES=ISOLATED_STORAGE)
class DocumentEndpointTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        user = get_user_model().objects.create_user("tester", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}
        ProfessionalProfile.objects.create(legal_name="Madelyn Spalding", master_resume=MASTER_RESUME)
        posting = IngestedPosting.objects.create(
            source="apify:indeed", title="Director of Development",
            company_name="AHA", url="https://example.test/job/2",
            raw_payload={"descriptionText": "x" * 500}, generated_materials=MATERIALS,
        )
        self.application = promote_posting_to_application(posting)

    def test_render_then_download(self):
        render = self.client.post(
            reverse("application-documents", args=[self.application.pk]), **self.auth
        )
        self.assertEqual(render.status_code, 200)

        response = self.client.get(
            reverse("application-download", args=[self.application.pk, "resume"]), **self.auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response.streaming_content)[:4], b"%PDF")

    def test_download_requires_authentication(self):
        # These carry her full contact details and a letter written for one
        # employer — they must never be reachable without a token.
        url = reverse("application-download", args=[self.application.pk, "cover-letter"])
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_missing_document_is_a_404_not_a_crash(self):
        url = reverse("application-download", args=[self.application.pk, "resume"])
        self.assertEqual(self.client.get(url, **self.auth).status_code, 404)
