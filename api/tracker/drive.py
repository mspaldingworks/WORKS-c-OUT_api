"""
Mirrors the generated application PDFs into a Google Drive folder.

Now per-user: each account can connect their own Drive (identity.GoogleDriveConnection)
and their PDFs go there. The server's OAuth credential stays as the *owner's* Drive
(and the owner's Sheet), used as a fallback for the owner account only — a user
without their own connection never has their drafts land in someone else's Drive.

The API serves these PDFs over token auth, which is right for the app but useless
at an employer's upload dialog — Drive is where they need to be to get attached.

Same contract as sheets.py: never raises into a user action. A failed upload must
not lose an application she just prepared — the PDF is still on the server.
"""

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Server credential scopes (Drive uploads + the owner's Sheet). Per-user
# connections only ever hold drive.file — see identity/google_oauth.py.
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


class DriveUnavailable(Exception):
    """Drive sync isn't configured or couldn't be reached."""


def user_credentials():
    """
    The server's OAuth credentials (the owner's). Used by the owner's Sheet sync
    (sheets.py) and as the owner's Drive fallback (resolve_drive).

    One credential acting as the owner. Service accounts can't own files in a
    consumer Drive at all, and using one for Sheets meant every new sheet had to
    be shared with a robot address first.
    """
    missing = [
        name for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.GOOGLE_OAUTH_CLIENT_ID),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.GOOGLE_OAUTH_CLIENT_SECRET),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", settings.GOOGLE_OAUTH_REFRESH_TOKEN),
        ) if not value
    ]
    if missing:
        raise DriveUnavailable(f"Google access isn't configured (needs {', '.join(missing)}).")

    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )


def credentials_for(refresh_token):
    """OAuth credentials for one user's Drive, built from their refresh token.

    Uses the per-user Web OAuth client (the one that minted the token) — NOT the
    server owner client, which may be a different Desktop client. Auto-refreshes
    on use like user_credentials().
    """
    client_id = settings.GOOGLE_DRIVE_OAUTH_CLIENT_ID or settings.GOOGLE_OAUTH_CLIENT_ID
    client_secret = settings.GOOGLE_DRIVE_OAUTH_CLIENT_SECRET or settings.GOOGLE_OAUTH_CLIENT_SECRET
    if not (client_id and client_secret):
        raise DriveUnavailable("Google Drive OAuth client isn't configured on the server.")

    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=DRIVE_SCOPES,
    )


def _is_default_owner(owner):
    if owner is None:
        return False
    try:
        from identity.owners import get_default_owner

        return owner.pk == get_default_owner().pk
    except Exception:
        return False


def resolve_drive(owner):
    """
    Where THIS owner's PDFs should go: (credentials, folder_id), or None to skip.

    Never anywhere when the account keeps documents in WORKS(c)OUT only.
    Otherwise: their own connected + enabled Drive; else — for the owner
    account ONLY — the server's Drive, so the owner's setup keeps working and
    no one else's drafts ever land in it; else nothing (the PDF stays on the
    server).
    """
    from identity.models import DocumentSettings, GoogleDriveConnection

    if DocumentSettings.storage_for(owner) == DocumentSettings.Storage.APP:
        return None

    connection = GoogleDriveConnection.objects.filter(owner=owner, enabled=True).first()
    if connection and connection.refresh_token_encrypted and connection.folder_id:
        try:
            token = connection.get_token()
        except Exception:
            logger.warning("Could not decrypt Drive token for owner %s", getattr(owner, "pk", "?"))
            token = ""
        if token:
            return credentials_for(token), connection.folder_id

    if (_is_default_owner(owner) and settings.GOOGLE_OAUTH_REFRESH_TOKEN
            and settings.JOB_DRIVE_FOLDER_ID):
        return user_credentials(), settings.JOB_DRIVE_FOLDER_ID

    return None


def _session(credentials):
    import google.auth.transport.requests

    return google.auth.transport.requests.AuthorizedSession(credentials)


def ensure_folder(credentials, name="WORKS(c)OUT Applications"):
    """
    Find (or create) the app's folder in this user's Drive; returns its id.

    drive.file scope only sees files the app itself created, so a folder the app
    made before is still findable — this stays stable across reconnects.
    """
    session = _session(credentials)
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (f"name = '{escaped}' and mimeType = 'application/vnd.google-apps.folder' "
             "and trashed = false")
    response = session.get(FILES_URL, params={"q": query, "fields": "files(id,name)"}, timeout=30)
    response.raise_for_status()
    files = response.json().get("files", [])
    if files:
        return files[0]["id"]

    response = session.post(
        FILES_URL, params={"fields": "id"},
        json={"name": name, "mimeType": "application/vnd.google-apps.folder"}, timeout=30,
    )
    response.raise_for_status()
    return response.json()["id"]


def _existing_file_id(session, folder_id, name):
    """
    Find a file of this name already in the folder.

    Drive allows duplicate names in one folder, so uploading blindly would leave
    a new copy on every regeneration and she'd have no idea which is current.
    """
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{escaped}' and '{folder_id}' in parents and trashed = false"
    response = session.get(FILES_URL, params={"q": query, "fields": "files(id,name)"}, timeout=30)
    response.raise_for_status()
    files = response.json().get("files", [])
    return files[0]["id"] if files else None


def upload_pdf(name, data, credentials, folder_id):
    """
    Put one PDF in the folder, replacing any previous version of the same name.

    Returns the Drive webViewLink.
    """
    return upload_file(name, data, credentials, folder_id).get("webViewLink", "")


def upload_file(name, data, credentials, folder_id, mime_type="application/pdf"):
    """
    Put one file in the folder, replacing any previous file of the same name.
    Returns Drive's {"id", "webViewLink"}.
    """
    session = _session(credentials)
    existing = _existing_file_id(session, folder_id, name)

    metadata = {"name": name}
    if not existing:
        metadata["parents"] = [folder_id]

    # Multipart upload: metadata part then the file bytes, per Drive's API.
    boundary = "worksscoutboundary"
    body = b"".join([
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
        json.dumps(metadata).encode(),
        f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode(),
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
    return response.json()


def upload_pdf_quietly(name, data, owner):
    """
    Fire-and-forget upload to the owner's resolved Drive, for use inside a user
    action. Returns a link, or '' — including when they have no connected Drive,
    in which case the PDF is still on the server and downloadable through the API.
    """
    resolved = resolve_drive(owner)
    if resolved is None:
        logger.info("Drive upload skipped for %s: no connected Drive", name)
        return ""

    credentials, folder_id = resolved
    try:
        link = upload_pdf(name, data, credentials, folder_id)
        logger.info("Uploaded %s to Drive", name)
        return link
    except DriveUnavailable as error:
        logger.warning("Drive upload skipped: %s", error)
    except Exception:
        logger.exception("Drive upload failed for %s", name)
    return ""


# What a picked file may be. Google Docs are exported to Word so they go
# through the same checks and text extraction as an uploaded .docx.
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMPORTABLE_MIME_TYPES = {
    "application/pdf": "",
    DOCX_MIME: "",
    "text/plain": "",
    "text/markdown": "",
    "application/rtf": "",
    "text/rtf": "",
    GOOGLE_DOC: ".docx",
}


def access_token(credentials):
    """A fresh short-lived access token, for the Picker running in the browser."""
    import google.auth.transport.requests

    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def file_metadata(credentials, file_id):
    """id, name, mimeType and size of one file — 404s unless the app may see it.

    Under drive.file that means a file the app created, or one the person
    picked in the Picker; nothing else in their Drive is reachable."""
    response = _session(credentials).get(
        f"{FILES_URL}/{file_id}", params={"fields": "id,name,mimeType,size,trashed,webViewLink"}, timeout=30,
    )
    if response.status_code == 404:
        raise DriveUnavailable("That file isn't available to WORKS(c)OUT — pick it again from Drive.")
    if response.status_code >= 400:
        raise DriveUnavailable(f"Drive refused ({response.status_code}).")
    return response.json()


def download_document(credentials, file_id, max_bytes):
    """
    Returns (filename, bytes) for one picked document, exporting a Google Doc
    to .docx. Refuses anything that isn't a document, or is over `max_bytes`.
    """
    meta = file_metadata(credentials, file_id)
    mime = meta.get("mimeType", "")
    if mime not in IMPORTABLE_MIME_TYPES:
        raise DriveUnavailable(f"{meta.get('name', 'That file')} isn't a PDF, Word or Google Doc.")
    if int(meta.get("size") or 0) > max_bytes:
        raise DriveUnavailable(f"{meta.get('name', 'That file')} is too large.")

    session = _session(credentials)
    if mime == GOOGLE_DOC:
        response = session.get(f"{FILES_URL}/{file_id}/export", params={"mimeType": DOCX_MIME},
                               timeout=60, stream=True)
    else:
        response = session.get(f"{FILES_URL}/{file_id}", params={"alt": "media"}, timeout=60, stream=True)
    if response.status_code >= 400:
        raise DriveUnavailable(f"Couldn't download {meta.get('name', 'that file')} ({response.status_code}).")

    data = b""
    for chunk in response.iter_content(64 * 1024):
        data += chunk
        if len(data) > max_bytes:
            raise DriveUnavailable(f"{meta.get('name', 'That file')} is too large.")

    name = meta.get("name") or "document"
    suffix = IMPORTABLE_MIME_TYPES[mime]
    if suffix and not name.lower().endswith(suffix):
        name += suffix
    return name, data


def folder_metadata(credentials, folder_id):
    meta = file_metadata(credentials, folder_id)
    if meta.get("mimeType") != GOOGLE_FOLDER or meta.get("trashed"):
        raise DriveUnavailable("That isn't a folder in your Drive.")
    return meta


def drive_credentials(owner):
    """
    Credentials that can read this owner's own Drive files back — whatever
    their storage choice now, since a file saved to Drive earlier still has to
    be readable. Their connected Drive, else (owner only) the server's.
    """
    from identity.models import GoogleDriveConnection

    connection = GoogleDriveConnection.objects.filter(owner=owner).first()
    if connection and connection.refresh_token_encrypted:
        return credentials_for(connection.get_token())
    if _is_default_owner(owner) and settings.GOOGLE_OAUTH_REFRESH_TOKEN:
        return user_credentials()
    return None


def download_file(credentials, file_id, max_bytes):
    """Bytes of one file the app may see, exporting a Google Doc to .docx."""
    return download_document(credentials, file_id, max_bytes)[1]
