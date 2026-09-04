"""
Mirrors the generated application PDFs into a Google Drive folder.

The API serves these over token auth, which is right for the app but useless
when she's sitting at an employer's upload dialog. Drive is where they need to
be to actually get attached to an application.

Same contract as sheets.py: never raises into a user action. A failed upload
must not lose an application she just prepared — the PDF is still on the server
and still downloadable through the API.
"""

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# drive.file only: access is limited to files this app creates, so the stored
# credentials can never read the rest of her Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


class DriveUnavailable(Exception):
    """Drive sync isn't configured or couldn't be reached."""


def _session():
    """
    Authorised session acting as the user, NOT as the service account.

    Service accounts have no Drive storage quota of their own, so anything they
    create in a consumer account's Drive is rejected outright — Google's own
    workarounds (shared drives, domain-wide delegation) both need Workspace.
    A user refresh token sidesteps that: files are created by her, owned by her,
    and counted against her quota.
    """
    missing = [
        name for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.GOOGLE_OAUTH_CLIENT_ID),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.GOOGLE_OAUTH_CLIENT_SECRET),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", settings.GOOGLE_OAUTH_REFRESH_TOKEN),
            ("JOB_DRIVE_FOLDER_ID", settings.JOB_DRIVE_FOLDER_ID),
        ) if not value
    ]
    if missing:
        raise DriveUnavailable(f"Drive upload isn't configured (needs {', '.join(missing)}).")

    import google.auth.transport.requests
    from google.oauth2.credentials import Credentials

    credentials = Credentials(
        token=None,
        refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    return google.auth.transport.requests.AuthorizedSession(credentials)


def _existing_file_id(session, name):
    """
    Find a file of this name already in the folder.

    Drive allows duplicate names in one folder, so uploading blindly would leave
    a new copy on every regeneration and she'd have no idea which is current.
    """
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{escaped}' and '{settings.JOB_DRIVE_FOLDER_ID}' in parents and trashed = false"
    response = session.get(FILES_URL, params={"q": query, "fields": "files(id,name)"}, timeout=30)
    response.raise_for_status()
    files = response.json().get("files", [])
    return files[0]["id"] if files else None


def upload_pdf(name, data):
    """
    Put one PDF in the folder, replacing any previous version of the same name.

    Returns the Drive webViewLink.
    """
    session = _session()
    existing = _existing_file_id(session, name)

    metadata = {"name": name}
    if not existing:
        metadata["parents"] = [settings.JOB_DRIVE_FOLDER_ID]

    # Multipart upload: metadata part then the file bytes, per Drive's API.
    boundary = "familyappilyboundary"
    body = b"".join([
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
        json.dumps(metadata).encode(),
        f"\r\n--{boundary}\r\nContent-Type: application/pdf\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
    params = {"uploadType": "multipart", "fields": "id,webViewLink"}

    if existing:
        response = session.patch(f"{UPLOAD_URL}/{existing}", params=params, headers=headers,
                                 data=body, timeout=120)
    else:
        response = session.post(UPLOAD_URL, params=params, headers=headers, data=body, timeout=120)

    if response.status_code >= 400:
        raise DriveUnavailable(f"Drive rejected the upload ({response.status_code}): {response.text[:200]}")
    return response.json().get("webViewLink", "")


def upload_pdf_quietly(name, data):
    """Fire-and-forget upload for use inside a user action. Returns a link or ''."""
    try:
        link = upload_pdf(name, data)
        logger.info("Uploaded %s to Drive", name)
        return link
    except DriveUnavailable as error:
        logger.warning("Drive upload skipped: %s", error)
    except Exception:
        logger.exception("Drive upload failed for %s", name)
    return ""
