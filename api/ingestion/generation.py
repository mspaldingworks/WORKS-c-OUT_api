"""
Generates a tailored cover letter and resume for one job posting.

The bottleneck in applying isn't the click — it's rewriting materials so they
speak the posting's language. This does that work against her canonical
resume, and is deliberately conservative: it may reorder, reword, and
re-emphasize what she has actually done, and must not invent experience,
tools, or numbers. An inflated claim caught in a screen costs more than the
gap it papered over.
"""

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You write job application materials for one specific candidate.

Absolute rules:
- Use ONLY experience, employers, tools, dates, and numbers present in the \
candidate's background. Never invent or upgrade anything.
- If the posting requires something the candidate lacks, do NOT imply they have \
it. Name it in the GAPS section instead, with honest language they can use.
- The COVER_LETTER must never concede, apologize for, or enumerate what the \
candidate lacks. No "I want to be straight with you about fit", no paragraph \
listing what they haven't done, no "if that trade is worth a conversation". \
Shortcomings belong in GAPS, which only the candidate sees. The letter makes \
the positive case and stops there.
- Prefer the candidate's own phrasing and real metrics over generic claims.
- Write in first person for the cover letter, plain confident prose. No \
"I am writing to express my interest", no filler, no superlatives about \
oneself, no em-dashes.
- The resume section is plain text the candidate will paste into a document.

Return exactly these four sections, each on its own line with the marker alone:

===COVER_LETTER===
<the letter>
===RESUME_SUMMARY===
<3-5 sentence professional summary tailored to this posting>
===RESUME_BULLETS===
<8-12 bullets, one per line, each starting with "- ", reordered and reworded \
to mirror the posting's priorities. Lead with the most relevant.>
===GAPS===
<one line per gap: what the posting asks for, what the candidate actually has, \
and the honest sentence they can say about it>
"""

SECTION_PATTERN = re.compile(
    r"===COVER_LETTER===(?P<cover_letter>.*?)"
    r"===RESUME_SUMMARY===(?P<resume_summary>.*?)"
    r"===RESUME_BULLETS===(?P<resume_bullets>.*?)"
    r"===GAPS===(?P<gaps>.*)",
    re.DOTALL,
)


class GenerationUnavailable(Exception):
    """Raised when materials can't be generated — missing key, resume, or API failure."""


def _strip_marker(line):
    """Drop a leading list marker ("- ", "* ", "• ") so the UI can style its own."""
    return line.strip().lstrip("-*•").strip()


def _posting_brief(posting):
    payload = posting.raw_payload or {}
    salary = payload.get("salary") or {}
    location = payload.get("location") or {}
    lines = [
        f"Job title: {posting.title}",
        f"Employer: {posting.company_name or 'Not stated'}",
        f"Location: {location.get('formattedAddressShort') if isinstance(location, dict) else location or 'Not stated'}",
        f"Remote: {payload.get('isRemote')}",
        f"Salary: {salary.get('salaryText') or 'Not stated'}",
        "",
        "Full posting text:",
        str(payload.get("descriptionText") or "")[:14000],
    ]
    return "\n".join(lines)


def generate_materials(posting, master_resume):
    """
    Returns a dict with cover_letter, resume_summary, resume_bullets, gaps.
    Raises GenerationUnavailable with a message suitable for showing the user.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise GenerationUnavailable(
            "No Anthropic API key configured on the server, so materials can't be generated yet."
        )
    if not master_resume.strip():
        raise GenerationUnavailable(
            "No master resume saved in Identity — add one before generating materials."
        )

    description = str((posting.raw_payload or {}).get("descriptionText") or "")
    if len(description) < 200:
        raise GenerationUnavailable(
            "This posting didn't include enough description text to tailor against."
        )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        # Streamed, not a plain create(): thinking plus a 16k budget runs well past
        # the SDK's non-streaming ceiling, and a long single response is exactly
        # what trips request timeouts. get_final_message() still hands back one
        # complete message, so nothing downstream has to care that it streamed.
        with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "CANDIDATE BACKGROUND (the only facts you may use):\n"
                    f"{master_resume}\n\n"
                    "----\n\n"
                    "JOB POSTING TO TAILOR FOR:\n"
                    f"{_posting_brief(posting)}"
                ),
            }],
        ) as stream:
            response = stream.get_final_message()
    except Exception as error:
        logger.exception("Anthropic call failed for posting %s", posting.pk)
        raise GenerationUnavailable(f"Couldn't reach the writing model: {error}") from error

    text = "".join(block.text for block in response.content if block.type == "text")
    match = SECTION_PATTERN.search(text)
    if not match:
        # Don't throw the work away over a formatting miss — hand back what came
        # out so it's still usable, and make the degraded state visible.
        logger.warning("Unparsed generation response for posting %s", posting.pk)
        return {
            "cover_letter": text.strip(),
            "resume_summary": "",
            "resume_bullets": [],
            "gaps": [],
            "unparsed": True,
        }

    bullets = [
        _strip_marker(line)
        for line in match.group("resume_bullets").splitlines()
        if line.strip().startswith("-")
    ]
    # Gaps aren't required to be bulleted (the model sometimes writes prose), so
    # take every non-empty line but still strip a marker when there is one —
    # otherwise the UI renders a stray dash in front of each item.
    gaps = [_strip_marker(line) for line in match.group("gaps").splitlines() if line.strip()]

    return {
        "cover_letter": match.group("cover_letter").strip(),
        "resume_summary": match.group("resume_summary").strip(),
        "resume_bullets": bullets,
        "gaps": gaps,
        "unparsed": False,
    }
