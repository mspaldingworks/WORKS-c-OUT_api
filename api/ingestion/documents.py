"""
Renders the generated materials into PDFs an employer portal will accept.

Portals want a file, not a text box, so without this the tailored writing can't
actually be submitted anywhere. Two documents per application: a cover letter,
and a resume that leads with the tailored summary and accomplishments and then
carries the real employment history underneath.

The history is deliberately NOT model-generated — it comes verbatim from the
master resume, because dates and employers are exactly the facts that must not
drift between one application and the next.
"""

import io
import logging
import re

from django.core.files.base import ContentFile
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer

logger = logging.getLogger(__name__)

# Sections lifted verbatim from master_resume.md. Anything not listed here
# (positioning notes, the generator's instructions) must never reach a PDF.
RESUME_SECTIONS = ["Current role", "Earlier roles", "Curatorial and grants record",
                   "Board and community leadership", "Education"]


def _styles():
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle("Name", parent=base["Title"], fontSize=20, spaceAfter=2, alignment=0),
        "contact": ParagraphStyle("Contact", parent=base["Normal"], fontSize=9.5, textColor="#444444",
                                  spaceAfter=14),
        "heading": ParagraphStyle("SectionHeading", parent=base["Heading2"], fontSize=11.5,
                                  spaceBefore=12, spaceAfter=5, textColor="#1B4F9C"),
        "body": ParagraphStyle("Body", parent=base["Normal"], fontSize=10, leading=14),
        "letter": ParagraphStyle("Letter", parent=base["Normal"], fontSize=10.5, leading=15.5,
                                 alignment=TA_JUSTIFY, spaceAfter=10),
        "bullet": ParagraphStyle("Bullet", parent=base["Normal"], fontSize=10, leading=13.5),
    }


def _escape(text):
    """reportlab treats its input as mini-HTML, so raw &, < and > break layout."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _contact_line(profile):
    if not profile:
        return ""
    parts = [profile.city_state, profile.phone, profile.email, profile.linkedin_url, profile.portfolio_url]
    return " · ".join(_escape(part) for part in parts if part)


def _build(story):
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    document.build(story)
    return buffer.getvalue()


def build_cover_letter_pdf(materials, profile):
    styles = _styles()
    name = _escape(profile.legal_name if profile else "")

    story = [Paragraph(name, styles["name"])]
    if contact := _contact_line(profile):
        story.append(Paragraph(contact, styles["contact"]))
    story.append(Spacer(1, 10))

    letter = materials.get("cover_letter", "")
    for block in [b.strip() for b in letter.split("\n\n") if b.strip()]:
        # Single newlines inside a paragraph are wrapping, not structure.
        story.append(Paragraph(_escape(" ".join(block.split("\n"))), styles["letter"]))

    return _build(story)


def _master_resume_sections(master_resume):
    """
    Pull the named sections out of the master resume markdown.

    Returns [(heading, [lines])]. Only headings in RESUME_SECTIONS are kept, so
    the positioning notes written for the generator can't leak onto a document
    an employer reads.
    """
    sections = []
    current = None
    for raw in (master_resume or "").splitlines():
        line = raw.rstrip()
        heading = re.match(r"^##\s+(.*)", line)
        if heading:
            title = heading.group(1).strip()
            current = (title, []) if title in RESUME_SECTIONS else None
            if current:
                sections.append(current)
            continue
        if current is not None and line.strip():
            current[1].append(line.strip())
    return sections


def _render_markdown_line(text, styles):
    """Handles the small subset of markdown the master resume actually uses."""
    escaped = _escape(re.sub(r"^[-*]\s+", "", text))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return Paragraph(escaped, styles["bullet"])


def build_resume_pdf(materials, profile):
    styles = _styles()
    story = [Paragraph(_escape(profile.legal_name if profile else ""), styles["name"])]
    if contact := _contact_line(profile):
        story.append(Paragraph(contact, styles["contact"]))

    if summary := materials.get("resume_summary"):
        story.append(Paragraph("SUMMARY", styles["heading"]))
        story.append(Paragraph(_escape(summary), styles["body"]))

    if bullets := materials.get("resume_bullets"):
        story.append(Paragraph("SELECTED ACCOMPLISHMENTS", styles["heading"]))
        story.append(ListFlowable(
            [ListItem(Paragraph(_escape(b), styles["bullet"]), leftIndent=12) for b in bullets],
            bulletType="bullet", start="•", leftIndent=14,
        ))

    for heading, lines in _master_resume_sections(profile.master_resume if profile else ""):
        story.append(Paragraph(heading.upper(), styles["heading"]))
        for line in lines:
            story.append(_render_markdown_line(line, styles))

    return _build(story)


def build_documents(application):
    """
    Render and attach both PDFs to an application. Returns the names written.

    Safe to call repeatedly — each call overwrites, so regenerating materials
    and re-rendering keeps the files in step with the text.
    """
    from identity.models import ProfessionalProfile

    posting = application.source_posting
    materials = (posting.generated_materials if posting else {}) or {}
    if not materials.get("cover_letter"):
        return []

    profile = ProfessionalProfile.objects.first()
    # 45 chars keeps the longest resulting path ("applications/cover_letters/"
    # + slug + "-cover-letter.pdf") inside FileField's 100-char max_length.
    # Over it, Django truncates and appends a random suffix on every save, so
    # the file could never be replaced in place and orphans accumulated.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{application.company.name}-{application.role_title}").strip("-")[:45]

    written = []
    letter_bytes = resume_bytes = None
    try:
        # Django uniquifies a colliding name rather than overwriting, so
        # re-rendering would leave the previous PDF orphaned on disk forever.
        for field in (application.cover_letter, application.resume):
            if field:
                field.delete(save=False)

        letter_bytes = build_cover_letter_pdf(materials, profile)
        resume_bytes = build_resume_pdf(materials, profile)

        application.cover_letter.save(f"{slug}-cover-letter.pdf", ContentFile(letter_bytes), save=False)
        application.resume.save(f"{slug}-resume.pdf", ContentFile(resume_bytes), save=False)
        application.save(update_fields=["cover_letter", "resume"])
        written = [application.cover_letter.name, application.resume.name]
    except Exception:
        # A failed render must not lose the application record itself.
        logger.exception("Couldn't render PDFs for application %s", application.pk)
        return written

    # Drive is where she reaches them from an employer's upload dialog; the API
    # copy is only useful inside the app. Failures here are logged, never raised
    # — the PDFs are already saved and downloadable either way.
    from tracker.drive import upload_pdf_quietly

    letter_url = upload_pdf_quietly(f"{slug}-cover-letter.pdf", letter_bytes)
    resume_url = upload_pdf_quietly(f"{slug}-resume.pdf", resume_bytes)
    if letter_url or resume_url:
        application.cover_letter_drive_url = letter_url
        application.resume_drive_url = resume_url
        application.save(update_fields=["cover_letter_drive_url", "resume_drive_url"])

    return written
