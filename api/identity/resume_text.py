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
