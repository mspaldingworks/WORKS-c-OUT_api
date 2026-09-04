"""
Normalizes job-posting items from different Apify Actors into IngestedPosting fields.

Each board's Actor returns a different shape (Indeed's `positionName` vs
LinkedIn's `title` vs Glassdoor's nested `company.name`, etc.), so this maps by
field aliases rather than keeping four hard-coded per-board mappers. Anything
not mapped to a column is preserved verbatim in `raw_payload`, so a mapping can
be improved later without re-scraping.
"""

import json
import re
import urllib.request

from .scoring import score_posting

TITLE_KEYS = ("title", "positionName", "jobTitle", "position", "name")
COMPANY_KEYS = ("companyName", "company", "employer", "companyInfo", "organization")
URL_KEYS = ("url", "jobUrl", "link", "detailsUrl", "jobLink")
APPLY_URL_KEYS = ("applyUrl", "applyLink", "jobUrl", "url")

# Model column limits — values are truncated to fit rather than blowing up the
# whole batch on one long field.
MAX_TITLE = 300
MAX_COMPANY = 200
MAX_URL = 1000

DATASET_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")


def _first_string(item, keys):
    """First non-empty string among `keys`, unwrapping {"name": ...} style nesting."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("name", "displayName", "title"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def normalize_item(item, source):
    """
    One Actor dataset item -> IngestedPosting field dict, or None if it has no
    usable title (better to skip than to store a blank row the user has to
    triage manually).
    """
    if not isinstance(item, dict):
        return None

    title = _first_string(item, TITLE_KEYS)
    if not title:
        return None

    url = _first_string(item, URL_KEYS)
    if len(url) > MAX_URL:
        # Truncating a URL makes it useless — drop it rather than store a broken
        # link. The original is still in raw_payload.
        url = ""

    # The employer's ATS link, kept separately so an Apply button can skip the
    # job-board listing and go where the form actually is.
    apply_url = _first_string(item, APPLY_URL_KEYS)
    if len(apply_url) > MAX_URL:
        apply_url = ""

    score, reasons = score_posting(item)

    return {
        "source": source,
        "title": title[:MAX_TITLE],
        "company_name": _first_string(item, COMPANY_KEYS)[:MAX_COMPANY],
        "url": url,
        "apply_url": apply_url,
        "raw_payload": item,
        "score": score,
        "score_reasons": reasons,
    }


def fetch_dataset_items(dataset_id, token="", limit=1000, timeout=30):
    """
    Pulls a finished run's items from Apify. Apify webhooks only carry run
    metadata, never the scraped items, so this second call is unavoidable.
    """
    if not DATASET_ID_PATTERN.match(dataset_id or ""):
        # The id arrives in an external webhook body — don't interpolate
        # anything unvalidated into an outbound URL.
        raise ValueError(f"Invalid Apify dataset id: {dataset_id!r}")

    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?clean=true&format=json&limit={limit}"
    if token:
        url += f"&token={token}"

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        payload = json.loads(response.read().decode("utf-8"))

    return payload if isinstance(payload, list) else []
