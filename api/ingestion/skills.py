"""
Works out which of her skills a posting asks for, and which it asks for that
she doesn't list.

Two separate questions, and the second is the useful one: a job matching twelve
of her skills still isn't worth much if it also wants four things she's never
touched. The feed shows both counts so a posting can be judged at a glance.

Matching is word-boundary only. Substring matching looks fine until "Git"
matches "digital" and "CSS" matches "success", and a feed that overstates her
fit is worse than one that says nothing.
"""

import re
from functools import lru_cache

# Extra search terms per skill, where the stored name isn't how a posting
# phrases it. The skill's own name is always searched too.
ALIASES = {
    "Fundraising": ["fundrais", "development officer", "annual fund"],
    "Grant Writing": ["grant writing", "grant writer", "grants management", "grant proposal"],
    "Donor Relations": ["donor relations", "donor stewardship", "major gifts",
                        "donor cultivation", "donor", "stewardship"],
    "Nonprofit Development": ["nonprofit development", "advancement", "philanthropy"],
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


def summarise(posting, skill_names):
    """
    Returns {matched: [...], missing: [...]} for one posting.

    `skill_names` is her stored skill list, passed in so the caller can read it
    once for a whole feed rather than per posting.
    """
    payload = posting.raw_payload or {}
    text = " ".join([
        str(payload.get("descriptionText") or "")[:20000],
        str(payload.get("title") or ""),
        " ".join(str(item) for item in (payload.get("requirements") or []) if item),
    ])
    if not text.strip():
        return {"matched": [], "missing": []}

    matched = [
        name for name in skill_names
        if _mentions(text, [name] + ALIASES.get(name, []))
    ]

    owned = {name.lower() for name in skill_names}
    missing = [
        label for label, terms in COMMON_REQUIREMENTS.items()
        if label.lower() not in owned and _mentions(text, terms)
    ]

    return {"matched": sorted(matched), "missing": sorted(missing)}
