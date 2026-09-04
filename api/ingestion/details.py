"""
Pulls the readable parts of a scraped payload into a fixed shape.

The feed used to ship each posting's entire raw_payload — every scraper
internal, both HTML and text copies of the description, geocoding — which made
the list response 710KB and still didn't give the app anything structured to
render. This sends the handful of things worth reading instead, so a job can be
judged inside the app rather than by opening the employer's page.
"""


def _text(value):
    return str(value).strip() if value not in (None, "") else ""


def _string_list(value, limit=12):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)][:limit]


def _location(payload):
    location = payload.get("location")
    if isinstance(location, str):
        return _text(location)
    if not isinstance(location, dict):
        return ""
    for key in ("formattedAddressShort", "formattedAddressLong", "city"):
        if text := _text(location.get(key)):
            return text
    parts = [_text(location.get("city")), _text(location.get("postalCode"))]
    return ", ".join(part for part in parts if part)


def _salary(payload):
    salary = payload.get("salary")
    if not isinstance(salary, dict):
        return ""
    if text := _text(salary.get("salaryText")):
        return text
    low, high = salary.get("salaryMin"), salary.get("salaryMax")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"${int(low):,} – ${int(high):,}"
    if isinstance(high, (int, float)):
        return f"Up to ${int(high):,}"
    return ""


def _rating(payload):
    rating = payload.get("rating")
    if not isinstance(rating, dict):
        return ""
    score, count = rating.get("rating"), rating.get("count")
    if not isinstance(score, (int, float)):
        return ""
    if isinstance(count, int) and count:
        return f"{score:.1f} from {count} review{'s' if count != 1 else ''}"
    return f"{score:.1f}"


def describe(posting):
    """Everything the expanded card in the feed shows."""
    payload = posting.raw_payload or {}
    return {
        # The full text, not a preview: the point is not having to leave the app,
        # and it's still smaller than the raw payload this replaced.
        "description": _text(payload.get("descriptionText")),
        "location": _location(payload),
        "salary": _salary(payload),
        "job_types": _string_list(payload.get("jobType")),
        "benefits": _string_list(payload.get("benefits"), limit=20),
        "requirements": _string_list(payload.get("requirements"), limit=20),
        "shifts": _string_list(payload.get("shifts")),
        "posted": _text(payload.get("age")),
        "is_remote": bool(payload.get("isRemote")),
        "company_rating": _rating(payload),
    }
