"""
Extracts plain text from an uploaded résumé file, so it can be handed to the
AI parser as prose. PDF and DOCX only — validate_resume_file already rejects
anything else before a file reaches here.
"""

import io

from docx import Document
from pypdf import PdfReader


class TextExtractionFailed(Exception):
    """Raised when a file can't be read as text — corrupt, or a scanned image with no text layer."""


def extract_resume_text(file):
    """Returns the résumé's plain text. `file` is any Django File-like object."""
    file.seek(0)
    data = file.read()
    file.seek(0)

    name = (file.name or "").lower()
    try:
        if name.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            document = Document(io.BytesIO(data))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception as error:
        raise TextExtractionFailed(f"Couldn't read this file: {error}") from error

    text = text.strip()
    if not text:
        raise TextExtractionFailed(
            "No readable text found in this file — it may be a scanned image with no text layer."
        )
    return text
