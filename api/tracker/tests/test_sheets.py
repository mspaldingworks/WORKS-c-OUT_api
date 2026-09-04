from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from ingestion.models import IngestedPosting
from ingestion.services import promote_posting_to_application
from tracker.sheets import HEADERS, MAX_CELL, SheetUnavailable, sync_sheet, sync_sheet_quietly


def make_application(cover_letter="Dear Hiring Team, ..."):
    posting = IngestedPosting.objects.create(
        source="apify:indeed",
        title="Digital Fundraising Strategy Lead",
        company_name="American Heart Association",
        url="https://example.test/job/1",
        apply_url="https://example.test/job/1/apply",
        score=100,
        raw_payload={"descriptionText": "x" * 500, "location": {"city": "Louisville"}},
        generated_materials={
            "cover_letter": cover_letter,
            "resume_summary": "Development leader.",
            "resume_bullets": ["Grew Give for Good"],
            "gaps": ["No PMP.", "No Workday experience."],
            "unparsed": False,
        },
    )
    return promote_posting_to_application(posting)


class SheetConfigurationTests(TestCase):
    @override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="", JOB_SHEET_ID="")
    def test_unconfigured_raises_a_readable_error(self):
        with self.assertRaises(SheetUnavailable) as caught:
            sync_sheet()
        self.assertIn("GOOGLE_SERVICE_ACCOUNT_FILE", str(caught.exception))

    @override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="", JOB_SHEET_ID="")
    def test_quiet_sync_never_raises_into_a_user_action(self):
        # She pressed "prepare", not "write a spreadsheet" — a missing sheet
        # config must not surface as a failure of the thing she asked for.
        self.assertIsNone(sync_sheet_quietly())


@override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="/tmp/key.json", JOB_SHEET_ID="sheet-123")
class SheetSyncTests(TestCase):
    def sync_with_mock(self):
        worksheet = MagicMock()
        with patch("gspread.service_account") as service_account:
            service_account.return_value.open_by_key.return_value.worksheet.return_value = worksheet
            count = sync_sheet()
        return worksheet, count

    def test_writes_a_header_row_and_one_row_per_application(self):
        make_application()
        worksheet, count = self.sync_with_mock()

        self.assertEqual(count, 1)
        rows = worksheet.update.call_args.kwargs["values"]
        self.assertEqual(rows[0], HEADERS)
        self.assertEqual(len(rows), 2)

        row = dict(zip(HEADERS, rows[1]))
        self.assertEqual(row["Score"], 100)
        self.assertEqual(row["Company"], "American Heart Association")
        self.assertEqual(row["Status"], "Saved")
        self.assertEqual(row["Apply URL"], "https://example.test/job/1/apply")
        self.assertIn("Dear Hiring Team", row["Cover letter"])
        self.assertIn("No PMP.", row["Gaps"])
        self.assertEqual(row["Location"], "Louisville")

    def test_clears_before_writing_so_removed_rows_do_not_linger(self):
        make_application()
        worksheet, _ = self.sync_with_mock()
        worksheet.clear.assert_called_once()

    def test_a_403_explains_how_to_fix_it(self):
        # Link-sharing lets the service account read but not write, so this is
        # the failure she'll actually hit. It has to name the fix.
        import gspread

        response = MagicMock()
        response.status_code = 403
        error = gspread.exceptions.APIError(response)
        error.response = response

        with patch("gspread.service_account") as service_account:
            service_account.return_value.open_by_key.side_effect = error
            with self.assertRaises(SheetUnavailable) as caught:
                sync_sheet()

        message = str(caught.exception)
        self.assertIn("Editor", message)
        self.assertIn("link-sharing", message.lower())

    def test_long_cover_letters_are_truncated_below_the_cell_limit(self):
        # Sheets rejects the whole write if any cell is over its limit, which
        # would lose the entire sync rather than one field.
        make_application(cover_letter="x" * (MAX_CELL + 5000))
        worksheet, _ = self.sync_with_mock()
        letter = dict(zip(HEADERS, worksheet.update.call_args.kwargs["values"][1]))["Cover letter"]
        self.assertLessEqual(len(letter), MAX_CELL)
