"""
Ranks ingested job postings against Madelyn's actual profile.

Indeed fuzzy-matches even quoted queries, so a "director of development" search
returns nursing and construction roles. Scoring is what makes the feed usable:
the top of the list should be worth reading, and the noise should sink.

Every posting also gets human-readable `reasons`, so a rank can be argued with
rather than taken on faith — if something scores high for the wrong reason,
that's visible and fixable here instead of being a black box.
"""

# (patterns, points, lane label) — the five lanes agreed with her, weighted by
# how central each is to her current work.
LANE_MATCHES = [
    (["director of development", "development director", "development manager",
      "major gifts", "annual fund", "donor relations", "advancement director",
      "fundraising", "philanthropy", "development officer", "gift officer"], 40, "development"),
    (["grant writer", "grants manager", "grant manager", "grants coordinator",
      "grants specialist", "grant specialist", "grants administrator",
      "sponsored programs", "grant coordinator"], 40, "grants"),
    (["communications manager", "communications director", "communications coordinator",
      "communications specialist", "marketing manager", "marketing coordinator",
      "content manager", "public relations", "digital communications"], 32, "communications"),
    (["web content", "digital content", "web manager", "webmaster", "wordpress",
      "digital marketing", "seo specialist", "content strategist",
      "web coordinator", "digital specialist"], 32, "web/digital"),
    (["program manager", "program director", "program coordinator",
      "youth program", "community program", "program administrator"], 28, "programs"),
]

# Roles that keep surfacing through Indeed's loose matching and are plainly not
# hers. A hard penalty rather than a filter, so nothing is silently hidden.
OFF_TARGET = [
    "nurse", "nursing", "rn/lpn", "lpn", "cna", "physician", "dental", "dentist",
    "pharmacy", "pharmacist", "caregiver", "phlebotom", "radiolog", "surgical",
    "truck", "cdl", "driver", "warehouse", "forklift", "construction", "electrician",
    "plumber", "hvac", "welder", "machinist", "mechanic", "roofing", "landscap",
    "janitor", "custodian", "housekeep", "security guard", "cashier", "line cook",
    "server", "dishwasher", "barista", "retail associate", "stocker",
]

# Title words that mean the role isn't paid employment. Checked against the
# title only: descriptions routinely say "manage volunteers" or "recruit interns"
# in roles that are perfectly good paid jobs.
UNPAID_TITLES = ["volunteer", "unpaid", "pro bono", "intern ", "internship"]

# Deliberately narrow. An earlier version included "community" and "youth",
# which appear in nearly every job description ("our community of users") and
# scored Canon, Amentum, and Healogics as mission-driven employers.
NONPROFIT_TEXT_SIGNALS = [
    "nonprofit", "non-profit", "not-for-profit", "501(c)", "philanthrop",
    "united way", "charitable organization", "our mission is to",
]

# Checked against the employer name only, where these words actually mean something.
NONPROFIT_NAME_SIGNALS = [
    "foundation", "coalition", "ministries", "council", "society", "association",
    "league", "ymca", "ywca", "goodwill", "habitat for humanity", "salvation army",
    "boys & girls", "boys and girls", "united way", "museum", "public library",
]

# Things she actually does, worth points when the description mentions them.
SKILL_SIGNALS = [
    "fundrais", "grant", "donor", "stewardship", "crm", "salesforce", "raiser's edge",
    "little green light", "wordpress", "google analytics", "campaign", "social media",
    "email marketing", "newsletter", "canva", "adobe", "volunteer", "stakeholder",
    "board of directors", "budget", "kpi", "storytelling", "copywriting", "seo",
    "event planning", "partnership",
]

SENIOR_TITLES = ["director", "manager", "lead", "head of", "chief"]
JUNIOR_TITLES = ["intern", "internship", "assistant", "entry level", "apprentice", "trainee"]

# Louisville metro, including the southern Indiana side she'd realistically commute to.
METRO_CITIES = [
    "louisville", "jeffersontown", "shively", "st matthews", "saint matthews", "prospect",
    "middletown", "fern creek", "okolona", "pleasure ridge park", "valley station",
    "jeffersonville", "new albany", "clarksville", "sellersburg", "charlestown",
    "floyds knobs", "georgetown", "shelbyville", "la grange", "lagrange", "mount washington",
    "shepherdsville", "crestwood", "pewee valley", "simpsonville", "taylorsville",
]

MAX_SCORE = 100


def _text_of(payload):
    parts = [
        str(payload.get("title") or ""),
        str(payload.get("companyName") or ""),
        str(payload.get("descriptionText") or "")[:6000],
    ]
    return " ".join(parts).lower()


def _location_points(payload):
    if payload.get("isRemote") is True:
        return 15, "Remote"

    location = payload.get("location")
    city = state = ""
    if isinstance(location, dict):
        city = str(location.get("city") or "").lower()
        state = str(location.get("formattedAddressShort") or "").lower()
    elif isinstance(location, str):
        city = location.lower()

    haystack = f"{city} {state}"
    if "remote" in haystack:
        return 15, "Remote"
    if any(metro in haystack for metro in METRO_CITIES):
        return 15, "Louisville area"
    if " ky" in haystack or " in" in haystack or "kentucky" in haystack or "indiana" in haystack:
        return 4, "Kentucky/Indiana, outside the metro"
    if haystack.strip():
        return -12, "Outside commuting range and not remote"
    return 0, None


def _freshness_points(payload):
    age = str(payload.get("age") or "").lower()
    if payload.get("postedToday") or "today" in age or "just posted" in age:
        return 8, "Posted today"
    digits = "".join(ch for ch in age if ch.isdigit())
    if digits and "day" in age:
        days = int(digits)
        if days <= 2:
            return 8, f"Posted {days} days ago"
        if days <= 7:
            return 4, f"Posted {days} days ago"
    return 0, None


def _salary_points(payload):
    salary = payload.get("salary")
    if not isinstance(salary, dict):
        return 0, None
    # An hourly wage is a strong signal this isn't a director-level role — it
    # avoids having to invent a target salary she never gave us.
    if str(salary.get("salaryType") or "").lower() == "hourly":
        return -10, "Hourly wage role"
    maximum = salary.get("salaryMax") or salary.get("salaryMin")
    if isinstance(maximum, (int, float)) and maximum >= 60000:
        return 6, f"Salary listed up to ${int(maximum):,}"
    return 0, None


def score_posting(payload):
    """Returns (score 0-100, list of human-readable reasons)."""
    if not isinstance(payload, dict):
        return 0, []

    text = _text_of(payload)
    title = str(payload.get("title") or "").lower()
    score = 25  # neutral baseline so a bare posting isn't scored at zero
    reasons = []

    best_lane_points, best_lane = 0, None
    for patterns, points, lane in LANE_MATCHES:
        if any(pattern in title for pattern in patterns):
            if points > best_lane_points:
                best_lane_points, best_lane = points, lane
        elif any(pattern in text for pattern in patterns) and points // 3 > best_lane_points:
            best_lane_points, best_lane = points // 3, f"{lane} (mentioned, not in title)"
    if best_lane:
        score += best_lane_points
        reasons.append(f"Matches your {best_lane} lane")

    if any(word in title for word in OFF_TARGET):
        score -= 45
        reasons.append("Looks like a clinical/trades/retail role — probably noise")

    # Unpaid work is not a job. "Volunteer Grant Writer" scored a perfect 100
    # because it matches the grants lane on every keyword — it just doesn't pay.
    # Recorded here but applied at the very end: zeroing the running total mid
    # function doesn't work, because every rule below would add points back on.
    is_unpaid = any(word in title for word in UNPAID_TITLES)
    if is_unpaid:
        reasons.append("Unpaid or volunteer position")

    company = str(payload.get("companyName") or "").lower()
    if any(signal in text for signal in NONPROFIT_TEXT_SIGNALS) or \
       any(signal in company for signal in NONPROFIT_NAME_SIGNALS):
        score += 10
        reasons.append("Nonprofit or mission-driven employer")

    matched_skills = {signal for signal in SKILL_SIGNALS if signal in text}
    if matched_skills:
        skill_points = min(len(matched_skills) * 2, 12)
        score += skill_points
        reasons.append(f"{len(matched_skills)} of your skills appear in the description")

    if any(word in title for word in SENIOR_TITLES):
        score += 8
        reasons.append("Director/manager level")
    if any(word in title for word in JUNIOR_TITLES):
        score -= 10
        reasons.append("Junior or assistant level")

    for points, reason in (_location_points(payload), _freshness_points(payload), _salary_points(payload)):
        score += points
        if reason:
            reasons.append(reason)

    if is_unpaid:
        # No amount of lane fit makes an unpaid post a job she can take.
        return 0, reasons

    return max(0, min(MAX_SCORE, score)), reasons
