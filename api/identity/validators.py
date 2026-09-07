"""
Server-side limits on résumé uploads.

A client-side check is not a limit — anyone can call the API directly with
curl, so every constraint here has to hold at the model/serializer boundary,
not just in the Swift app's file picker.

The accepted formats and the byte-level checks live in `document_intake`,
which is also what actually reads the file. Keeping one list means a format
can never be storable but unreadable, or readable but refused — which is
exactly what happened when this module and the intake module disagreed.
"""

from django.core.exceptions import ValidationError

from .document_intake import (
    ALLOWED_EXTENSIONS,
    MAX_DOCUMENT_BYTES,
    _extension,
    looks_like,
)

MAX_RESUME_BYTES = MAX_DOCUMENT_BYTES
MAX_RESUMES_PER_OWNER = 10

FORMATS_SENTENCE = "PDF, DOCX, TXT, Markdown and RTF files are accepted."


def validate_resume_file(file):
    """
    Raises ValidationError unless `file` is a real document under the size cap.

    Checks the extension AND the file's actual bytes — a renamed .exe with a
    .pdf extension must not pass just because the client claims it's a PDF.
    """
    name = (file.name or "").lower()
    extension = _extension(name)
    if not extension:
        raise ValidationError(FORMATS_SENTENCE)

    if file.size > MAX_RESUME_BYTES:
        raise ValidationError(f"Résumé files can't exceed {MAX_RESUME_BYTES // (1024 * 1024)}MB.")

    file.seek(0)
    data = file.read()
    file.seek(0)

    if not looks_like(extension, data):
        raise ValidationError(
            f"This doesn't look like a real {extension.lstrip('.').upper()} file."
        )
