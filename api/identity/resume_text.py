"""
Extracts plain text from an uploaded résumé file, so it can be handed to the
AI parser as prose.

The reading itself lives in `document_intake`, shared with the stateless
review endpoint — one extractor for one set of accepted formats, so nothing
can be storable but unreadable.
"""

from .document_intake import DocumentUnreadable, extract_document_text


class TextExtractionFailed(Exception):
    """Raised when a file can't be read as text — corrupt, or a scanned image with no text layer."""


def extract_resume_text(file):
    """Returns the résumé's plain text. `file` is any Django File-like object."""
    file.seek(0)
    data = file.read()
    file.seek(0)
    try:
        return extract_document_text(data, file.name or "")
    except DocumentUnreadable as error:
        raise TextExtractionFailed(str(error)) from error


# How much of a cover letter drafts see as a voice sample. A letter is a page;
# this is room for one with a little to spare.
WRITING_SAMPLE_CHARS = 6000


def writing_sample_for(owner):
    """
    The text of this account's newest cover letter, or "" — what drafts read
    to match the person's own voice. Never a source of facts. A letter that
    can't be read is skipped rather than failing the draft.
    """
    from .models import ResumeVersion

    letter = (
        ResumeVersion.objects.filter(owner=owner, kind=ResumeVersion.Kind.COVER_LETTER,
                                     discarded_at__isnull=True)
        .order_by("-created_at").first()
    )
    if letter is None:
        return ""
    from .document_storage import document_text

    try:
        # Wherever it's kept — here, or only in their Drive.
        return document_text(letter)[:WRITING_SAMPLE_CHARS]
    except Exception:
        return ""
