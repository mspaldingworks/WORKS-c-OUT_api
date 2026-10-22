"""
Workspace-less Claude keys, picking documents and a save folder from Drive,
and cover letters as a voice sample for drafts.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity import llm
from identity.models import GoogleDriveConnection, LLMCredential, ResumeVersion
from identity.resume_text import writing_sample_for
from tracker.drive import DriveUnavailable

PICKER = {"GOOGLE_PICKER_API_KEY": "picker-key", "GOOGLE_CLOUD_PROJECT_NUMBER": "123456",
          "GOOGLE_DRIVE_OAUTH_CLIENT_ID": "cid", "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET": "secret"}


def make_user(name="docs@example.test"):
    user = get_user_model().objects.create_user(name, email=name, password="pw-123456789")
    return user, {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}


def pdf_bytes(text="Ten years of grant writing and fundraising for nonprofits."):
    import io

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, text)
    pdf.save()
    return buffer.getvalue()


class WorkspaceKeyTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user()

    def _save(self, **body):
        return self.client.post(reverse("llmcredential-list"), data={"provider": "anthropic", **body},
                                content_type="application/json", **self.auth)

    def test_workspace_id_is_saved_and_sent(self):
        self.assertEqual(self._save(api_key="sk-ant-x", workspace_id="wrkspc_01AbCdEf").status_code, 201)
        config = llm.resolve_config(self.user)
        self.assertEqual(config.workspace_id, "wrkspc_01AbCdEf")
        with patch("anthropic.Anthropic") as client:
            stream = client.return_value.messages.stream.return_value.__enter__.return_value
            stream.get_final_message.return_value = MagicMock(content=[])
            llm.complete("s", "u", config=config)
        self.assertEqual(client.call_args.kwargs["default_headers"], {"anthropic-workspace-id": "wrkspc_01AbCdEf"})

    def test_no_workspace_means_no_header(self):
        self._save(api_key="sk-ant-x")
        with patch("anthropic.Anthropic") as client:
            stream = client.return_value.messages.stream.return_value.__enter__.return_value
            stream.get_final_message.return_value = MagicMock(content=[])
            llm.complete("s", "u", config=llm.resolve_config(self.user))
        self.assertIsNone(client.call_args.kwargs["default_headers"])

    def test_a_workspace_can_be_added_without_pasting_the_key_again(self):
        self._save(api_key="sk-ant-original")
        self.assertEqual(self._save(workspace_id="wrkspc_01AbCdEf").status_code, 201)
        cred = LLMCredential.objects.get(owner=self.user)
        self.assertEqual(cred.get_key(), "sk-ant-original")
        self.assertEqual(cred.workspace_id, "wrkspc_01AbCdEf")

    def test_a_first_save_still_needs_a_key(self):
        self.assertEqual(self._save(workspace_id="wrkspc_01AbCdEf").status_code, 400)

    def test_a_malformed_workspace_id_is_refused(self):
        self.assertEqual(self._save(api_key="sk-ant-x", workspace_id="default").status_code, 400)

    def test_the_raw_workspace_error_becomes_an_instruction(self):
        error = Exception("Error code: 400 - This API key is not scoped to a workspace, so this request must ...")
        with patch("anthropic.Anthropic") as client:
            client.return_value.messages.stream.side_effect = error
            with self.assertRaises(llm.LLMUnavailable) as raised:
                llm.complete("s", "u", config=llm.LLMConfig("anthropic", "k", "m", ""))
        self.assertEqual(str(raised.exception), llm.NO_WORKSPACE_MESSAGE)


@override_settings(**PICKER)
class PickerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user, self.auth = make_user()
        self.connection = GoogleDriveConnection(owner=self.user, folder_id="app-folder")
        self.connection.set_token("refresh-token")
        self.connection.save()

    def _link(self, mode="documents"):
        return self.client.get(reverse("drive-picker-link") + f"?mode={mode}", **self.auth)

    def test_link_needs_a_connection(self):
        self.connection.delete()
        self.assertEqual(self._link().status_code, 409)

    @override_settings(GOOGLE_PICKER_API_KEY="")
    def test_link_says_when_the_server_is_not_set_up(self):
        self.assertEqual(self._link().status_code, 503)

    @patch("identity.drive_picker._credentials", return_value=object())
    @patch("tracker.drive.access_token", return_value="ya29.short-lived")
    def test_page_carries_a_token_once(self, _token, _creds):
        url = self._link().json()["picker_url"]
        path = urlparse(url).path + "?" + urlparse(url).query
        first = self.client.get(path)
        self.assertEqual(first.status_code, 200)
        self.assertIn("ya29.short-lived", first.content.decode())
        self.assertEqual(first["Cache-Control"], "no-store")
        second = self.client.get(path)
        self.assertNotIn("ya29.short-lived", second.content.decode())
        self.assertIn("already+used", second.content.decode())

    def test_a_forged_ticket_gets_no_token(self):
        response = self.client.get(reverse("drive-picker") + "?t=forged")
        self.assertIn("workscout://drive-picked", response.content.decode())
        self.assertNotIn("token", response.content.decode().split("drive-picked")[0].lower())

    def test_settings_report_picker_availability(self):
        body = self.client.get(reverse("drive-connection"), **self.auth).json()
        self.assertTrue(body["picker_available"])
        self.assertEqual(body["folder_name"], "")


@override_settings(**PICKER)
class ImportTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user()
        connection = GoogleDriveConnection(owner=self.user, folder_id="app-folder")
        connection.set_token("refresh-token")
        connection.save()
        for target, value in (("identity.drive_picker._credentials", object()),
                              ("tracker.drive.file_metadata", {"webViewLink": "https://drive.test/f"})):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _import(self, ids, kind="resume"):
        return self.client.post(reverse("drive-import"), data={"file_ids": ids, "kind": kind},
                                content_type="application/json", **self.auth)

    @patch("tracker.drive.download_document", return_value=("CV 2026.pdf", pdf_bytes()))
    def test_a_picked_resume_becomes_a_document(self, _download):
        response = self._import(["drive-file-1"])
        self.assertEqual(response.status_code, 201)
        created = response.json()["created"]
        self.assertEqual(created[0]["title"], "CV 2026")
        self.assertEqual(created[0]["kind"], "resume")
        self.assertEqual(ResumeVersion.objects.filter(owner=self.user).count(), 1)
        # Picked from Drive, so Drive already has it — the original is the link.
        self.assertEqual(created[0]["drive_url"], "https://drive.test/f")
        self.assertEqual(created[0]["stored_in"], "both")

    @patch("tracker.drive.download_document", return_value=("CV.pdf", pdf_bytes()))
    def test_drive_only_keeps_just_the_original_in_drive(self, _download):
        from identity.models import DocumentSettings

        DocumentSettings.objects.create(owner=self.user, storage="drive")
        created = self._import(["drive-file-1"]).json()["created"]
        self.assertEqual(created[0]["stored_in"], "drive")
        self.assertFalse(ResumeVersion.objects.get(owner=self.user).file)

    @patch("tracker.drive.download_document", return_value=("Letter.pdf", pdf_bytes()))
    def test_cover_letters_are_kept_as_cover_letters(self, _download):
        self._import(["drive-file-1"], kind="cover_letter")
        self.assertEqual(ResumeVersion.objects.get(owner=self.user).kind, "cover_letter")

    @patch("tracker.drive.download_document", return_value=("fake.pdf", b"not really a pdf"))
    def test_a_file_that_is_not_what_it_claims_is_refused(self, _download):
        response = self._import(["drive-file-1"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], [])
        self.assertTrue(response.json()["refused"][0]["detail"])

    @patch("tracker.drive.download_document", side_effect=DriveUnavailable("Not a PDF, Word or Google Doc."))
    def test_drive_refusals_are_reported_per_file(self, _download):
        response = self._import(["a"])
        self.assertEqual(response.json()["refused"][0]["detail"], "Not a PDF, Word or Google Doc.")

    def test_unknown_kind_is_refused(self):
        self.assertEqual(self._import(["a"], kind="portfolio").status_code, 400)

    @patch("tracker.drive.folder_metadata", return_value={"id": "f-9", "name": "Job hunt 2026"})
    def test_choosing_a_save_folder(self, _meta):
        response = self.client.post(reverse("drive-folder"), data={"folder_id": "f-9"},
                                    content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["folder_name"], "Job hunt 2026")
        connection = GoogleDriveConnection.objects.get(owner=self.user)
        self.assertEqual(connection.folder_id, "f-9")

    @patch("tracker.drive.ensure_folder", return_value="app-folder-2")
    def test_going_back_to_the_app_folder(self, _ensure):
        self.client.post(reverse("drive-folder"), data={"folder_id": ""},
                         content_type="application/json", **self.auth)
        connection = GoogleDriveConnection.objects.get(owner=self.user)
        self.assertEqual((connection.folder_id, connection.folder_name), ("app-folder-2", ""))


class CoverLetterTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user()

    def _letter(self, text="I build donor programs that last."):
        return ResumeVersion.objects.create(
            owner=self.user, kind="cover_letter", title="Letter",
            file=SimpleUploadedFile("letter.pdf", pdf_bytes(text)),
        )

    def test_cover_letters_are_not_read_for_skills(self):
        letter = self._letter()
        response = self.client.post(reverse("resumeversion-parse", args=[letter.pk]), **self.auth)
        self.assertEqual(response.status_code, 400)

    def test_the_newest_cover_letter_is_the_voice_sample(self):
        self._letter("An older letter.")
        self._letter("I build donor programs that last.")
        self.assertIn("donor programs", writing_sample_for(self.user))

    def test_no_cover_letter_means_no_sample(self):
        self.assertEqual(writing_sample_for(self.user), "")

    def test_drafts_see_the_sample_as_voice_not_facts(self):
        from ingestion.generation import generate_materials

        posting = MagicMock(pk=1, title="Grant Writer", company_name="Org")
        with patch("ingestion.generation.payload_of", return_value={"descriptionText": "x" * 300}), \
             patch("identity.llm.complete", return_value="") as complete:
            generate_materials(posting, "My history.", config=llm.LLMConfig("anthropic", "k", "m", ""),
                               writing_sample="I build donor programs that last.")
        prompt = complete.call_args.args[1]
        self.assertIn("I build donor programs that last.", prompt)
        self.assertIn("NOT a source of facts", prompt)
