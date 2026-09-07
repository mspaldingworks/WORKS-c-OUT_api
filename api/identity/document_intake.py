"""
Reads plain text out of an uploaded document so it can be handed to the AI
parser.

This is the wider-net sibling of `resume_text.py`. That module serves the
Identity tab's own résumé library, where PDF and DOCX are the only formats
`validate_resume_file` lets through. This one backs the stateless review
endpoint used by other apps, whose members upload whatever they have on their
phone — so it also takes plain text, Markdown and RTF, which need no parsing
library and carry no macro/embedded-object risk.

Format checks read the file's actual bytes, not just its extension: a renamed
executable must not get as far as a parser because the client claimed it was
a PDF.
"""

import io
import re
import zipfile

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown", ".rtf")

PDF_MAGIC = b"%PDF-"
DOCX_MARKER = "[Content_Types].xml"
RTF_MAGIC = b"{\\rt"

# RTF is a plain-text container: control words start with a backslash, groups
# are braces. Stripping both leaves the prose, which is all the parser needs —
# well short of a real RTF reader, and enough that a résumé exported from
# TextEdit or Word parses instead of being rejected.
RTF_CONTROL_WORD = re.compile(r"\\\*?\\?[a-zA-Z]{1,32}(-?\d{1,10})?[ ]?")
RTF_HEX_ESCAPE = re.compile(r"\\'[0-9a-fA-F]{2}")


class DocumentUnreadable(Exception):
    """Raised when a file can't be read as text — wrong type, corrupt, or image-only."""


def _extension(name):
    lowered = (name or "").lower()
    for extension in ALLOWED_EXTENSIONS:
        if lowered.endswith(extension):
            return extension
    return ""


def _decode(data):
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise DocumentUnreadable("This file isn't readable as text.")


def _rtf_to_text(data):
    body = _decode(data)
    body = RTF_HEX_ESCAPE.sub("", body)
    body = RTF_CONTROL_WORD.sub(" ", body)
    body = body.replace("{", " ").replace("}", " ")
    return re.sub(r"[ \t]{2,}", " ", body)


def _pdf_to_text(data):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _docx_to_text(data):
    from docx import Document

    document = Document(io.BytesIO(data))
    parts = [paragraph.text for paragraph in document.paragraphs]
    # Plenty of résumés lay their dates and employers out in a table, which
    # `paragraphs` walks straight past; skipping them would silently drop the
    # work history the parser is being asked to read.
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def looks_like(extension, data):
    """Whether `data`'s own bytes match the format its extension claims."""
    if extension == ".pdf":
        return data.startswith(PDF_MAGIC)
    if extension == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return DOCX_MARKER in archive.namelist()
        except zipfile.BadZipFile:
            return False
    if extension == ".rtf":
        return data.lstrip()[:4].startswith(RTF_MAGIC)
    # Text and Markdown have no signature; decodability is the only check
    # available, and _decode makes it.
    return True


def extract_document_text(data, filename):
    """Returns the document's plain text, or raises DocumentUnreadable."""
    if not data:
        raise DocumentUnreadable("The uploaded file was empty.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentUnreadable(
            f"Documents can't exceed {MAX_DOCUMENT_BYTES // (1024 * 1024)}MB."
        )

    extension = _extension(filename)
    if not extension:
        raise DocumentUnreadable(
            "Supported formats are PDF, DOCX, TXT, Markdown and RTF."
        )
    if not looks_like(extension, data):
        raise DocumentUnreadable(
            f"This doesn't look like a real {extension.lstrip('.').upper()} file."
        )

    try:
        if extension == ".pdf":
            text = _pdf_to_text(data)
        elif extension == ".docx":
            text = _docx_to_text(data)
        elif extension == ".rtf":
            text = _rtf_to_text(data)
        else:
            text = _decode(data)
    except DocumentUnreadable:
        raise
    except Exception as error:
        raise DocumentUnreadable(f"Couldn't read this file: {error}") from error

    text = text.strip()
    if not text:
        raise DocumentUnreadable(
            "No readable text found in this file — it may be a scan with no text layer."
        )
    return text
