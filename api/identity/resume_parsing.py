"""
Parses an uploaded résumé's text into structured skill and profile
suggestions.

Uses the same Claude calling pattern as ingestion/generation.py — adaptive
thinking, a streamed response so a long generation doesn't trip a request
timeout — because that pattern is already proven here. Unlike generation.py's
delimited text sections, the output here is a JSON object: what comes back is
inherently a list of records (skills), not prose.

This never writes anything by itself. The result is staged on the
ResumeVersion row for review; a skill only becomes a real Skill row when the
Identity tab sends its own POST to /api/identity/skills/ — the same endpoint
a manual addition uses. Nothing here can invent experience she didn't upload,
and nothing here can silently overwrite her canonical profile either.
"""

import json
import logging

from django.conf import settings

from . import llm

logger = logging.getLogger(__name__)

VALID_PROFICIENCIES = {"learning", "competent", "strong", "expert"}
MAX_SKILLS = 40

SYSTEM_PROMPT = """You extract structured facts from ONE résumé's text, for a candidate to review before anything is saved.

Absolute rules:
- Use ONLY what is actually written in the résumé text. Never infer a skill, \
title, employer, date, or proficiency level that isn't stated or clearly \
demonstrated by the text (e.g. "led a Python migration" supports "Python", \
but do not add related tools the text never names).
- If you are unsure whether something counts, leave it out rather than guess. \
A missed skill costs nothing; a fabricated one costs credibility with an \
employer who asks about it.
- proficiency is a coarse guess from context (how the résumé talks about it), \
not a certification claim — always one of: learning, competent, strong, expert. \
Default to "competent" when the text gives no signal either way.
- category is a short label you choose (e.g. "Software", "Fundraising", \
"Design tools") to group related skills — reuse the same category text for \
skills that clearly belong together.
- headline is a one-line professional title, only if the résumé states or \
strongly implies one (e.g. its own header line). Empty string if genuinely unclear.

Return ONLY a single JSON object, no prose before or after, matching exactly:

{
  "skills": [{"name": str, "category": str, "proficiency": "learning"|"competent"|"strong"|"expert"}],
  "headline": str,
  "email": str,
  "phone": str,
  "linkedin_url": str
}

Leave any field "" (or skills: []) rather than guess. Cap skills at 40 — pick \
the ones most clearly demonstrated, not every tool mentioned once in passing.
"""


class ParsingUnavailable(Exception):
    """Raised when a résumé can't be parsed — missing key, thin text, or an API failure."""


def parse_resume_text(text, config=None):
    """
    Returns a dict with skills (list of {name, category, proficiency}),
    headline, email, phone, linkedin_url, unparsed. `config` is the account's
    chosen LLM provider (identity.llm.resolve_config); None uses the server's
    Anthropic key. Raises ParsingUnavailable with a message for the user.
    """
    if config is None and not settings.ANTHROPIC_API_KEY:
        raise ParsingUnavailable(
            "No AI provider is configured. Add your own API key in Identity → AI provider."
        )
    if len(text.strip()) < 50:
        raise ParsingUnavailable("This résumé didn't have enough readable text to parse.")

    try:
        raw = llm.complete(
            SYSTEM_PROMPT,
            f"RÉSUMÉ TEXT:\n\n{text[:20000]}",
            config=config,
            max_tokens=8000,
        )
    except llm.LLMUnavailable as error:
        logger.exception("Résumé parse failed")
        raise ParsingUnavailable(str(error)) from error

    return _parse_json_leniently(raw.strip())


def _parse_json_leniently(raw):
    """
    Best-effort JSON extraction. The model is asked for JSON only, but
    sometimes wraps it in a code fence or adds a stray sentence — don't throw
    the work away over a formatting miss, same philosophy as
    generation.py's SECTION_PATTERN fallback.
    """
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end != -1:
        candidate = candidate[start:end + 1]

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Unparsed résumé-parse response")
        return {
            "skills": [], "headline": "", "email": "", "phone": "", "linkedin_url": "",
            "unparsed": True, "raw": raw,
        }

    skills = []
    for item in (data.get("skills") or [])[: MAX_SKILLS * 2]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        proficiency = str(item.get("proficiency") or "competent").strip().lower()
        if proficiency not in VALID_PROFICIENCIES:
            proficiency = "competent"
        skills.append({"name": name, "category": str(item.get("category") or "").strip(),
                        "proficiency": proficiency})

    return {
        "skills": skills[:MAX_SKILLS],
        "headline": str(data.get("headline") or "").strip(),
        "email": str(data.get("email") or "").strip(),
        "phone": str(data.get("phone") or "").strip(),
        "linkedin_url": str(data.get("linkedin_url") or "").strip(),
        "unparsed": False,
    }
