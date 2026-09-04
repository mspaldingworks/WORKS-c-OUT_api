from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from tracker.drive import DriveUnavailable, upload_pdf, upload_pdf_quietly

PDF = b"%PDF-1.4 fake"


class DriveConfigurationTests(SimpleTestCase):
    @override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="",
                       GOOGLE_OAUTH_REFRESH_TOKEN="", JOB_DRIVE_FOLDER_ID="")
    def test_unconfigured_names_every_missing_setting(self):
        with self.assertRaises(DriveUnavailable) as caught:
            upload_pdf("x.pdf", PDF)
        message = str(caught.exception)
        for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_REFRESH_TOKEN", "JOB_DRIVE_FOLDER_ID"):
            self.assertIn(name, message)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="",
                       GOOGLE_OAUTH_REFRESH_TOKEN="", JOB_DRIVE_FOLDER_ID="")
    def test_quiet_upload_never_raises_into_a_user_action(self):
        self.assertEqual(upload_pdf_quietly("x.pdf", PDF), "")

    def test_scope_is_limited_to_files_this_app_creates(self):
        # Full "drive" scope would let a long-lived stored token read her entire
        # Drive; the uploader only ever needs to write its own files.
        from tracker.drive import SCOPES
        self.assertEqual(SCOPES, ["https://www.googleapis.com/auth/drive.file"])


@override_settings(GOOGLE_OAUTH_CLIENT_ID="cid", GOOGLE_OAUTH_CLIENT_SECRET="secret",
                   GOOGLE_OAUTH_REFRESH_TOKEN="refresh", JOB_DRIVE_FOLDER_ID="folder-1")
class DriveUploadTests(TestCase):
    def make_session(self, existing=None):
        session = MagicMock()
        listing = MagicMock()
        listing.json.return_value = {"files": [{"id": existing}] if existing else []}
        listing.raise_for_status.return_value = None
        session.get.return_value = listing
        written = MagicMock()
        written.status_code = 200
        written.json.return_value = {"id": "new-id", "webViewLink": "https://drive.test/new-id"}
        session.post.return_value = written
        session.patch.return_value = written
        return session

    def test_creates_a_new_file_in_the_configured_folder(self):
        session = self.make_session()
        with patch("tracker.drive._session", return_value=session):
            link = upload_pdf("resume.pdf", PDF)

        self.assertEqual(link, "https://drive.test/new-id")
        session.post.assert_called_once()
        body = session.post.call_args.kwargs["data"]
        self.assertIn(b"folder-1", body)
        self.assertIn(PDF, body)

    def test_replaces_an_existing_file_of_the_same_name(self):
        # Drive allows duplicate names in a folder, so a blind upload would
        # leave a second copy every regeneration and she couldn't tell which
        # one an employer should get.
        session = self.make_session(existing="old-id")
        with patch("tracker.drive._session", return_value=session):
            upload_pdf("resume.pdf", PDF)

        session.post.assert_not_called()
        session.patch.assert_called_once()
        self.assertIn("old-id", session.patch.call_args[0][0])
        # A replacement must not re-parent the file.
        self.assertNotIn(b"parents", session.patch.call_args.kwargs["data"])

    def test_an_api_error_becomes_a_readable_failure(self):
        session = self.make_session()
        session.post.return_value.status_code = 403
        session.post.return_value.text = "insufficientFilePermissions"
        with patch("tracker.drive._session", return_value=session):
            with self.assertRaises(DriveUnavailable) as caught:
                upload_pdf("resume.pdf", PDF)
        self.assertIn("403", str(caught.exception))

    def test_quote_in_a_filename_cannot_break_the_search_query(self):
        session = self.make_session()
        with patch("tracker.drive._session", return_value=session):
            upload_pdf("O'Brien Foundation-resume.pdf", PDF)
        query = session.get.call_args.kwargs["params"]["q"]
        self.assertIn("O\\'Brien", query)
