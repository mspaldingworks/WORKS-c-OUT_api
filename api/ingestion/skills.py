"""
Works out which of an account's skills a posting asks for, and which it asks
for that the account doesn't list.

Two separate questions, and the second is the useful one: a job matching twelve
of her skills still isn't worth much if it also wants four things she's never
touched. The feed shows both counts so a posting can be judged at a glance.

Matching is word-boundary only. Substring matching looks fine until "Git"
matches "digital" and "CSS" matches "success", and a feed that overstates her
fit is worse than one that says nothing.
"""

import re
from functools import lru_cache

from .details import payload_of

# Extra search terms per skill, where the stored name isn't how a posting
# phrases it. The skill's own name is always searched too.
ALIASES = {
    "Fundraising": ["fundrais", "development officer", "annual fund"],
    "Grant Writing": ["grant writing", "grant writer", "grants management", "grant proposal"],
    "Donor Relations": ["donor relations", "donor stewardship", "major gifts",
                        "donor cultivation", "donor", "stewardship"],
    "Nonprofit Development": ["nonprofit development", "advancement", "philanthropy",
                              "director of development", "development director",
                              "development manager"],
    "Google Analytics": ["google analytics", "ga4", "web analytics"],
    "Email Marketing": ["email marketing", "email campaign", "e-newsletter", "newsletter",
                        "email communications", "crm email"],
    "Campaign Strategy": ["campaign strategy", "campaign planning", "multi-channel campaign",
                          "campaign", "communications strategy", "communications plan",
                          "communications roadmap", "go-to-market"],
    "Copywriting": ["copywriting", "copy writing", "content writing", "writing",
                    "editorial", "storytelling", "messaging"],
    "Program Management": ["program management", "program manager", "program coordination"],
    "Staff Supervision": ["staff supervision", "supervise staff", "people management",
                          "manage a team", "direct reports", "lead a team", "mentor",
                          "manage staff", "supervisory"],
    "Event Planning": ["event planning", "event management", "event coordination"],
    "WordPress": ["wordpress"],
    "Graphic Design": ["graphic design", "visual design", "brand identity", "design assets"],
    "Video Production": ["video production", "video editing"],
    "Public Programming": ["public programming", "community programming",
                           "community engagement", "community outreach"],
    "Exhibition Design": ["exhibition design", "exhibit design"],
    "Curatorial Practice": ["curatorial", "curator"],
    "Arts Administration": ["arts administration", "arts management"],
    "JavaScript": ["javascript", "java script"],
    "CSS": ["css"],
    "HTML": ["html"],
    "Git": ["git", "github", "version control"],
    "Python": ["python"],
    "Django": ["django"],
    "Docker": ["docker"],
    "PHP": ["php"],
}

# Things postings in her lanes routinely ask for. Anything here that a posting
# mentions and she doesn't list becomes a gap — this is what makes "still
# lacking" computable without waiting on a generated letter.
COMMON_REQUIREMENTS = {
    "Salesforce": ["salesforce"],
    "Salesforce Marketing Cloud": ["marketing cloud"],
    "Raiser's Edge": ["raiser's edge", "raisers edge", "blackbaud"],
    "HubSpot": ["hubspot"],
    "Mailchimp": ["mailchimp"],
    "Tableau": ["tableau"],
    "Power BI": ["power bi", "powerbi"],
    "Looker Studio": ["looker studio", "data studio"],
    "SQL": ["sql"],
    "Excel": ["excel", "spreadsheet"],
    "Asana": ["asana"],
    "Jira": ["jira"],
    "Monday.com": ["monday.com"],
    "Figma": ["figma"],
    "Canva": ["canva"],
    "Adobe Creative Suite": ["adobe", "photoshop", "indesign", "illustrator"],
    "SEO": ["seo", "search engine optimization"],
    "Paid Media": ["google ads", "paid media", "paid search", "ppc"],
    "SMS Marketing": ["sms marketing", "text messaging platform"],
    "Peer-to-peer Fundraising": ["peer-to-peer", "peer to peer fundraising"],
    "Spanish": ["bilingual", "spanish"],
    "PMP": ["pmp", "project management professional"],
    "CFRE": ["cfre"],
    "Salesforce Admin": ["salesforce administrator"],
    "Workday": ["workday"],
    "Drupal": ["drupal"],
    "React": ["react.js", "reactjs"],
    "Google Ad Grants": ["ad grants"],
    "Videography": ["videography", "video shooting"],
    "Photography": ["photography", "photographer"],
}


@lru_cache(maxsize=512)
def _pattern(term):
    # \b around the whole phrase: substring matching makes "Git" hit "digital"
    # and "SQL" hit "MySQL-adjacent" prose, inflating her apparent fit.
    return re.compile(rf"\b{re.escape(term)}\b", re.I)


def _mentions(text, terms):
    return any(_pattern(term).search(text) for term in terms)


@lru_cache(maxsize=512)
def _skill_pattern(name):
    # One alternation per skill rather than one search per phrasing: rescoring
    # an account runs this over every posting it holds.
    terms = sorted({term.lower() for term in skill_terms(name) if term.strip()}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b", re.I)


def posting_text(payload):
    """The readable text of a scraped payload: description, title and any
    listed requirements. What both skill matching and fit scoring search."""
    if not isinstance(payload, dict):
        return ""
    # Usually a list, sometimes one string; anything else isn't readable text.
    requirements = payload.get("requirements")
    if isinstance(requirements, str):
        requirements = [requirements]
    elif not isinstance(requirements, list):
        requirements = []
    return " ".join([
        str(payload.get("descriptionText") or payload.get("description") or "")[:20000],
        str(payload.get("title") or ""),
        " ".join(str(item) for item in requirements if item),
    ])


def skill_terms(name):
    """A skill's own name plus any known phrasings of it."""
    return [name] + ALIASES.get(name, [])


def skills_mentioned(text, skill_names):
    """Which of `skill_names` the text asks for, word-boundary matched."""
    if not text.strip():
        return []
    return sorted(name for name in skill_names if _skill_pattern(name).search(text))


def summarise(posting, skill_names):
    """
    Returns {matched: [...], missing: [...]} for one posting.

    `skill_names` is the account's stored skill list, passed in so the caller
    can read it once for a whole feed rather than per posting.
    """
    text = posting_text(payload_of(posting))
    if not text.strip():
        return {"matched": [], "missing": []}

    matched = skills_mentioned(text, skill_names)

    owned = {name.lower() for name in skill_names}
    missing = [
        label for label, terms in COMMON_REQUIREMENTS.items()
        if label.lower() not in owned and _mentions(text, terms)
    ]

    return {"matched": matched, "missing": sorted(missing)}
