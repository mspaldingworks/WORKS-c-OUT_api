"""
Where an uploaded résumé or cover letter is kept, and reading it back.

Each account chooses (identity.DocumentSettings): WORKS(c)OUT and Drive, WORKS(c)OUT
only, or Drive only. A document always arrives as bytes here first — that's
where the format checks run — and `apply_storage` then puts it where the
account keeps documents. Nothing is ever dropped on a failure: if Drive can't
take a file, the copy here stays.

Reading goes through `document_bytes`, which takes the copy here when there is
one and fetches from Drive when there isn't, so parsing and the cover-letter
voice sample don't care where a document lives.
"""

import logging
from pathlib import PurePath

from .document_intake import DocumentUnreadable, extract_document_text
from .models import DocumentSettings, ResumeVersion
from .resume_text import TextExtractionFailed
from .validators import MAX_RESUME_BYTES

logger = logging.getLogger(__name__)

# How much of an uploaded cover letter the library shows without opening it.
PREVIEW_CHARS = 600

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rtf": "application/rtf",
}


class DocumentUnavailable(Exception):
    """The document's only copy is in Drive and Drive can't be reached."""


def extension_of(resume):
    return PurePath(resume.file.name).suffix.lower() if resume.file else ""


def download_name(resume):
    """What the file is called when it leaves: its title, not the random
    storage name."""
    return f"{resume.title or 'Document'}{extension_of(resume) or '.pdf'}"


def stored_in(resume):
    here, drive = bool(resume.file), bool(resume.drive_file_id)
    return "both" if here and drive else "drive" if drive else "app"


def document_bytes(resume):
    """(filename, bytes) from this server's copy, else from Drive."""
    if resume.file:
        resume.file.open("rb")
        try:
            return resume.file.name, resume.file.read()
        finally:
            resume.file.close()
    if resume.drive_file_id:
        from tracker.drive import DriveUnavailable, download_document, drive_credentials

        credentials = drive_credentials(resume.owner)
        if credentials is None:
            raise DocumentUnavailable(
                f"{resume.title} is kept in Google Drive, which isn't connected. Reconnect it in Identity."
            )
        try:
            return download_document(credentials, resume.drive_file_id, MAX_RESUME_BYTES)
        except DriveUnavailable as error:
            raise DocumentUnavailable(str(error)) from error
    raise DocumentUnavailable(f"{resume.title} has no file to read.")


def document_text(resume):
    """Plain text of the document, wherever it's kept."""
    name, data = document_bytes(resume)
    try:
        return extract_document_text(data, name)
    except DocumentUnreadable as error:
        raise TextExtractionFailed(str(error)) from error


def _remember_preview(resume, name, data):
    """Cover letters carry a short text preview for the library, read once
    here rather than on every list."""
    if resume.kind != ResumeVersion.Kind.COVER_LETTER:
        return
    try:
        text = extract_document_text(data, name)
    except DocumentUnreadable:
        return
    resume.parsed_data = {**(resume.parsed_data or {}), "preview": " ".join(text.split())[:PREVIEW_CHARS]}
    resume.save(update_fields=["parsed_data"])


def apply_storage(resume):
    """
    Put a just-created document where its account keeps documents.

    A document picked from Drive arrives with drive_file_id already set — the
    original is its Drive copy, so it is never uploaded a second time.
    """
    name, data = document_bytes(resume)
    _remember_preview(resume, name, data)

    storage = DocumentSettings.storage_for(resume.owner)
    if storage == DocumentSettings.Storage.APP:
        return resume

    if not resume.drive_file_id:
        from tracker.drive import resolve_drive, upload_file

        resolved = resolve_drive(resume.owner)
        if resolved is None:
            return resume
        credentials, folder_id = resolved
        try:
            uploaded = upload_file(download_name(resume), data, credentials, folder_id,
                                   MIME_TYPES.get(extension_of(resume), "application/octet-stream"))
        except Exception:
            # The copy here stays, so nothing is lost; it just isn't in Drive.
            logger.exception("Drive copy failed for document %s", resume.pk)
            return resume
        resume.drive_file_id = uploaded.get("id", "")
        resume.drive_url = uploaded.get("webViewLink", "")
        resume.save(update_fields=["drive_file_id", "drive_url"])

    if storage == DocumentSettings.Storage.DRIVE and resume.drive_file_id and resume.file:
        resume.file.delete(save=False)
        resume.file = ""
        resume.save(update_fields=["file"])
    return resume
