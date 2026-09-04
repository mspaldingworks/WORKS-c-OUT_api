"""
Checks whether a posting is still live, and retires the ones that aren't.

A feed full of filled roles wastes the most expensive thing here — her time
reading it, and the money spent generating materials for a job nobody can apply
to any more.

Deliberately conservative. Retiring a live posting is far worse than leaving a
dead one visible for another few days, so a posting is only retired on an
unambiguous signal: the page is gone, or it says in plain words that the role
is closed. Anything ambiguous — a timeout, a bot wall, a redirect somewhere
unexpected — is left alone and reported.
"""

import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# A browser UA: several ATS return 403 to anything that looks scripted, and a
# 403 is not evidence that a job is gone.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

GONE_STATUSES = {404, 410}

# Phrases that only appear when a listing is closed. Kept narrow and specific:
# "no longer" alone matches plenty of live descriptions.
CLOSED_MARKERS = [
    "no longer accepting applications",
    "no longer available",
    "this job is no longer",
    "position has been filled",
    "job posting has expired",
    "this posting has expired",
    "requisition is closed",
    "we are no longer accepting",
    "job not found",
    "the job you are looking for",
    "this job has been closed",
    "posting is no longer active",
]

TIMEOUT = 20


def check_url(url, opener=None):
    """
    Returns (verdict, detail) where verdict is "live", "gone" or "unknown".

    "unknown" is the safe default and covers everything that isn't proof —
    network trouble, bot walls, and any status that isn't explicitly a
    disappearance.
    """
    if not url:
        return "unknown", "no url"

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        opened = (opener or urllib.request.urlopen)(request, timeout=TIMEOUT)
        with opened as response:
            body = response.read(200_000).decode("utf-8", "ignore").lower()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        if error.code in GONE_STATUSES:
            return "gone", f"HTTP {error.code}"
        # 403 usually means a bot wall, not a removed job.
        return "unknown", f"HTTP {error.code}"
    except Exception as error:
        return "unknown", f"{type(error).__name__}: {error}"

    if status in GONE_STATUSES:
        return "gone", f"HTTP {status}"

    for marker in CLOSED_MARKERS:
        if marker in body:
            return "gone", f'page says "{marker}"'

    return "live", f"HTTP {status}"


def sweep(postings, opener=None, on_result=None):
    """
    Check each posting and retire the ones proven gone. Returns a summary.

    Only postings still awaiting triage are worth retiring — once she's applied,
    the record matters regardless of whether the ad is still up.
    """
    from .models import IngestedPosting

    summary = {"checked": 0, "retired": 0, "live": 0, "unknown": 0, "details": []}
    for posting in postings:
        url = posting.apply_url or posting.url
        verdict, detail = check_url(url, opener=opener)
        summary["checked"] += 1

        if verdict == "gone":
            posting.status = IngestedPosting.Status.EXPIRED
            posting.save(update_fields=["status"])
            summary["retired"] += 1
        else:
            summary[verdict] += 1

        summary["details"].append(
            {"id": posting.pk, "title": posting.title, "verdict": verdict, "detail": detail}
        )
        if on_result:
            on_result(posting, verdict, detail)

    return summary
