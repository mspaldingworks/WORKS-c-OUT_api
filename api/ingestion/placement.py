"""
Where and when a posting is: its working arrangement, its coordinates, and the
day it was listed.

These feed three Job-Feed tools — the Workplace filter (remote / hybrid /
on-site), the distance-from-home filter and sort, and "posted within" — so
they're derived once at ingest and stored as columns, like the salary facets.

Scrapers disagree on how they say any of this. Indeed sets `isRemote` but
leaves hybrid roles to free text ("Hybrid remote in Louisville, KY", a
"Hybrid work" attribute), and gives both an ISO `datePublished` and a relative
`age`. Anything unreadable stays blank rather than guessed: an unknown
arrangement is not "on-site", and a missing date is not "today".
"""

import datetime
import re

REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"

# "hybrid" on its own also means hybrid cloud, hybrid vehicles, hybrid events —
# in a description it has to sit next to a word about how the work is done.
_HYBRID_IN_PROSE = re.compile(
    r"\bhybrid\b[\s-]*(?:work|working|schedule|role|position|model|environment|"
    r"remote|arrangement|setting|opportunity|office)"
    r"|\b(?:work|working|schedule|role|position|model)\b[^.]{0,20}\bhybrid\b"
)
_REMOTE_IN_PROSE = re.compile(r"\b(?:fully|100%|completely)\s+remote\b|\bwork from home\b")
_DAYS_AGO = re.compile(r"(\d+)\+?\s*(day|week|month)")


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _location_text(item):
    location = item.get("location")
    if isinstance(location, str):
        return location
    if isinstance(location, dict):
        return " ".join(
            str(location.get(key) or "")
            for key in ("formattedAddressLong", "formattedAddressShort", "fullAddress", "city")
        )
    return ""


def work_arrangement(item):
    """remote / hybrid / onsite, or "" when the posting doesn't say."""
    if not isinstance(item, dict):
        return ""

    # Short, structured fields: any mention here is a statement about the job.
    labels = " ".join(
        [_location_text(item)]
        + _strings(item.get("workingSystem"))
        + _strings(item.get("attributes"))
        + _strings(item.get("jobType"))
    ).lower()
    # Titles say "Program Manager (Hybrid)" but also "Hybrid Cloud Architect",
    # so a title only counts as hybrid the way prose does, or in brackets.
    title = str(item.get("title") or "").lower()
    prose = str(item.get("descriptionText") or "")[:6000].lower()

    # Hybrid first: boards flag hybrid roles isRemote=true as often as not, and
    # a hybrid job still means a commute.
    if ("hybrid" in labels or "(hybrid)" in title
            or _HYBRID_IN_PROSE.search(title) or _HYBRID_IN_PROSE.search(prose)):
        return HYBRID
    if (item.get("isRemote") is True or "remote" in labels or "work from home" in labels
            or re.search(r"\bremote\b", title)):
        return REMOTE
    if _REMOTE_IN_PROSE.search(prose):
        return REMOTE
    if coordinates(item) != (None, None) or _location_text(item).strip():
        return ONSITE
    return ""


def coordinates(item):
    """(latitude, longitude) from the scraped location, or (None, None)."""
    location = item.get("location") if isinstance(item, dict) else None
    if not isinstance(location, dict):
        return None, None
    lat, lng = location.get("latitude"), location.get("longitude")
    if isinstance(lat, bool) or isinstance(lng, bool):
        return None, None
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        # 0,0 is the classic "geocoder gave up" value, not a job in the Atlantic.
        return None, None
    return lat, lng


def posted_on(item, reference_date=None):
    """
    The day the job was listed, or None.

    `datePublished` wins when it parses. Otherwise the relative `age` ("3 days
    ago", "30+ days ago", "Just posted") is resolved against `reference_date` —
    the day the posting was scraped, which is what "ago" was relative to.
    """
    if not isinstance(item, dict):
        return None
    reference_date = reference_date or datetime.date.today()

    published = item.get("datePublished") or item.get("postedAt") or item.get("postingDateParsed")
    if isinstance(published, str) and published.strip():
        try:
            return datetime.date.fromisoformat(published.strip()[:10])
        except ValueError:
            pass

    age = str(item.get("age") or "").lower()
    if item.get("postedToday") is True or "today" in age or "just posted" in age or "hour" in age:
        return reference_date
    match = _DAYS_AGO.search(age)
    if match:
        count, unit = int(match.group(1)), match.group(2)
        days = count * {"day": 1, "week": 7, "month": 30}[unit]
        return reference_date - datetime.timedelta(days=days)
    return None
