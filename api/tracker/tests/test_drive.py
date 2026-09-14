from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from tracker.drive import DriveUnavailable, upload_pdf, upload_pdf_quietly

PDF = b"%PDF-1.4 fake"


class DriveConfigurationTests(SimpleTestCase):
    def test_user_credentials_names_missing_settings(self):
        from tracker.drive import user_credentials

        with self.settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="",
                           GOOGLE_OAUTH_REFRESH_TOKEN=""):
            with self.assertRaises(DriveUnavailable) as caught:
                user_credentials()
        message = str(caught.exception)
        for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
            self.assertIn(name, message)

    def test_credentials_for_requires_the_oauth_client(self):
        from tracker.drive import credentials_for

        with self.settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET=""):
            with self.assertRaises(DriveUnavailable):
                credentials_for("some-refresh")

    def test_quiet_upload_returns_empty_when_no_drive(self):
        # No connected Drive resolves to None; the caller must not see an error.
        with patch("tracker.drive.resolve_drive", return_value=None):
            self.assertEqual(upload_pdf_quietly("x.pdf", PDF, owner=None), "")

    def test_quiet_upload_swallows_upload_errors(self):
        with patch("tracker.drive.resolve_drive", return_value=(MagicMock(), "folder-1")), \
             patch("tracker.drive.upload_pdf", side_effect=Exception("boom")):
            self.assertEqual(upload_pdf_quietly("x.pdf", PDF, owner=None), "")

    def test_scopes(self):
        from tracker.drive import DRIVE_SCOPES, SCOPES

        # Server credential: files the app creates + the owner's job sheet.
        self.assertEqual(SCOPES, [
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/spreadsheets",
        ])
        # Per-user connections only ever hold drive.file.
        self.assertEqual(DRIVE_SCOPES, ["https://www.googleapis.com/auth/drive.file"])


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

    def test_creates_a_new_file_in_the_given_folder(self):
        session = self.make_session()
        with patch("tracker.drive._session", return_value=session):
            link = upload_pdf("resume.pdf", PDF, credentials=MagicMock(), folder_id="folder-1")

        self.assertEqual(link, "https://drive.test/new-id")
        session.post.assert_called_once()
        body = session.post.call_args.kwargs["data"]
        self.assertIn(b"folder-1", body)
        self.assertIn(PDF, body)

    def test_replaces_an_existing_file_of_the_same_name(self):
        session = self.make_session(existing="old-id")
        with patch("tracker.drive._session", return_value=session):
            upload_pdf("resume.pdf", PDF, credentials=MagicMock(), folder_id="folder-1")

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
                upload_pdf("resume.pdf", PDF, credentials=MagicMock(), folder_id="folder-1")
        self.assertIn("403", str(caught.exception))

    def test_quote_in_a_filename_cannot_break_the_search_query(self):
        session = self.make_session()
        with patch("tracker.drive._session", return_value=session):
            upload_pdf("O'Brien Foundation-resume.pdf", PDF, credentials=MagicMock(), folder_id="folder-1")
        query = session.get.call_args.kwargs["params"]["q"]
        self.assertIn("O\\'Brien", query)
