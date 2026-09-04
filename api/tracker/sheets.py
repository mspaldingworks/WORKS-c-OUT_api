"""
Mirrors the application pipeline into a Google Sheet.

The app is the system of record; the sheet is a read-only projection of it, so
every sync rewrites the rows wholesale rather than trying to patch individual
cells. That keeps it correct even if someone edits the sheet by hand — their
edits are overwritten on the next sync, which is the intended behaviour for a
view rather than a data entry surface.

Never raises into a user action: a failed sheet write must not lose an
application she just prepared.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

WORKSHEET_TITLE = "Applications"

HEADERS = [
    "Score",
    "Title",
    "Company",
    "Location",
    "Status",
    "Applied date",
    "Job URL",
    "Apply URL",
    "Resume PDF",
    "Cover letter PDF",
    "Cover letter",
    "Resume summary",
    "Gaps",
    "Notes",
    "Last updated",
]

# Sheets rejects a cell over 50k characters and truncating mid-sync is worse
# than a slightly short letter, so clamp well under the limit.
MAX_CELL = 40000


class SheetUnavailable(Exception):
    """Sheet sync isn't configured or couldn't be reached."""


def _truncate(text):
    text = str(text or "")
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def _row_for(application):
    """One sheet row per Application, pulling materials off the source posting."""
    posting = getattr(application, "source_posting", None)
    materials = (posting.generated_materials if posting else {}) or {}
    payload = (posting.raw_payload if posting else {}) or {}

    location = payload.get("location")
    if isinstance(location, dict):
        location = location.get("formattedAddressShort") or location.get("city") or ""

    return [
        posting.score if posting else "",
        application.role_title,
        application.company.name if application.company_id else "",
        _truncate(location),
        application.get_status_display(),
        application.applied_date.isoformat() if application.applied_date else "",
        application.job_url,
        (posting.apply_url or posting.url) if posting else "",
        application.resume_drive_url,
        application.cover_letter_drive_url,
        _truncate(materials.get("cover_letter", "")),
        _truncate(materials.get("resume_summary", "")),
        _truncate("\n".join(materials.get("gaps", []) or [])),
        _truncate(application.notes),
        application.updated_at.strftime("%Y-%m-%d %H:%M"),
    ]


def _open_worksheet():
    if not settings.GOOGLE_SERVICE_ACCOUNT_FILE or not settings.JOB_SHEET_ID:
        raise SheetUnavailable(
            "Google Sheets sync isn't configured "
            "(needs GOOGLE_SERVICE_ACCOUNT_FILE and JOB_SHEET_ID)."
        )

    import gspread

    client = gspread.service_account(filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE)
    try:
        spreadsheet = client.open_by_key(settings.JOB_SHEET_ID)
        try:
            return spreadsheet.worksheet(WORKSHEET_TITLE)
        except gspread.WorksheetNotFound:
            return spreadsheet.add_worksheet(title=WORKSHEET_TITLE, rows=200, cols=len(HEADERS))
    except gspread.exceptions.APIError as error:
        # By far the most common setup mistake: link-sharing grants the service
        # account read access, so the sheet opens and only the write 403s. The
        # raw error says "caller does not have permission", which doesn't hint
        # at the fix, so name the account and the required role.
        if getattr(error, "response", None) is not None and error.response.status_code == 403:
            raise SheetUnavailable(
                f"The sheet is readable but not writable. Share it with "
                f"{_service_account_email()} as an Editor "
                f"(link-sharing alone only grants view access)."
            ) from error
        raise SheetUnavailable(f"Google rejected the request: {error}") from error


def _service_account_email():
    """Read the client_email out of the key file, for error messages."""
    import json

    try:
        with open(settings.GOOGLE_SERVICE_ACCOUNT_FILE) as handle:
            return json.load(handle).get("client_email", "the service account")
    except Exception:
        return "the service account"


def sync_sheet():
    """
    Rewrite the worksheet from current Application rows. Returns the row count.

    Raises SheetUnavailable — callers acting on a user's behalf should catch it
    and carry on, since the application record itself is already saved.
    """
    from .models import Application

    worksheet = _open_worksheet()

    applications = (
        Application.objects.select_related("company", "source_posting")
        .order_by("-updated_at")
    )
    rows = [HEADERS] + [_row_for(application) for application in applications]

    worksheet.clear()
    worksheet.update(values=rows, range_name="A1")
    return len(rows) - 1


def sync_sheet_quietly():
    """
    Fire-and-forget sync for use inside a user action. Logs and swallows every
    failure: she pressed a button to prepare applications, not to write a sheet,
    and the sheet is reconstructable from the database at any time.
    """
    try:
        count = sync_sheet()
        logger.info("Synced %s applications to the job sheet", count)
        return count
    except SheetUnavailable as error:
        logger.warning("Sheet sync skipped: %s", error)
    except Exception:
        logger.exception("Sheet sync failed")
    return None
