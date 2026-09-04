"""
Server-side limits on résumé uploads.

A client-side check is not a limit — anyone can call the API directly with
curl, so every constraint here has to hold at the model/serializer boundary,
not just in the Swift app's file picker.
"""

import zipfile

from django.core.exceptions import ValidationError

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10MB — comfortably above any real PDF/DOCX résumé.
MAX_RESUMES_PER_OWNER = 10
ALLOWED_EXTENSIONS = (".pdf", ".docx")

PDF_MAGIC = b"%PDF-"
# DOCX (OOXML) is a zip archive; this entry is present in every valid one and
# absent from a renamed .zip of something else — a cheap, dependency-free
# stand-in for real MIME sniffing.
DOCX_MARKER = "[Content_Types].xml"


def validate_resume_file(file):
    """
    Raises ValidationError unless `file` is a real PDF or DOCX under the size cap.

    Checks the extension AND the file's actual bytes — a renamed .exe with a
    .pdf extension must not pass just because the client claims it's a PDF.
    """
    name = (file.name or "").lower()
    if not name.endswith(ALLOWED_EXTENSIONS):
        raise ValidationError("Only PDF and DOCX résumés are accepted.")

    if file.size > MAX_RESUME_BYTES:
        raise ValidationError(f"Résumé files can't exceed {MAX_RESUME_BYTES // (1024 * 1024)}MB.")

    file.seek(0)
    header = file.read(8)
    file.seek(0)

    if name.endswith(".pdf"):
        if not header.startswith(PDF_MAGIC):
            raise ValidationError("This doesn't look like a real PDF file.")
        return

    try:
        with zipfile.ZipFile(file) as archive:
            if DOCX_MARKER not in archive.namelist():
                raise ValidationError("This doesn't look like a real DOCX file.")
    except zipfile.BadZipFile:
        raise ValidationError("This doesn't look like a real DOCX file.")
    finally:
        file.seek(0)
