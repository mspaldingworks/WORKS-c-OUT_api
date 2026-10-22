"""
Ranks a job posting against one account's own profile.

Everything a score rests on comes from the account itself: the skills it has
confirmed (usually from an uploaded résumé), and the home location and radius
it set in Identity. Nothing here assumes who the person is, what field they
work in or where they live — an account with no skills and no home gets a
neutral score that only reflects freshness and whether the job pays, which is
the honest answer when there's nothing to compare against.

Every posting also gets human-readable `reasons`, so a rank can be argued with
rather than taken on faith.
"""

import hashlib
import json
from dataclasses import dataclass, field

from .geo import miles_between
from .skills import posting_text, skills_mentioned

# Bump when the rules below change, so every account's stored scores are
# recomputed on its next feed load (see ingestion.fit.refresh_if_stale).
SCORING_VERSION = 2

MAX_SCORE = 100
# Where a posting starts before anything about the account is known: low
# enough that a strong skill match clearly separates, high enough that a fresh
# local job still beats a stale one when no skills are set.
BASELINE = 20

# Title words that mean the role isn't paid employment. Checked against the
# title only: descriptions routinely say "manage volunteers" or "recruit interns"
# in roles that are perfectly good paid jobs.
UNPAID_TITLES = ["volunteer", "unpaid", "pro bono"]


@dataclass(frozen=True)
class ScoringProfile:
    """What one account's scores are computed against."""

    skills: tuple = ()
    # (latitude, longitude), or None when no home location is saved.
    home: tuple | None = None
    radius_miles: int = 25

    def signature(self):
        """A short fingerprint of everything that affects a score. Stored per
        account; when it changes, that account's postings are rescored."""
        blob = json.dumps({
            "v": SCORING_VERSION,
            "skills": sorted(self.skills),
            "home": [round(value, 4) for value in self.home] if self.home else None,
            "radius": self.radius_miles,
        }, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()


BLANK_PROFILE = ScoringProfile()


@dataclass
class Fit:
    score: int
    reasons: list = field(default_factory=list)
    # Which of the account's skills the posting asks for. Stored on the row so
    # the feed's skill filter runs in the database.
    matched_skills: list = field(default_factory=list)


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


def _location_points(profile, is_remote, latitude, longitude):
    """
    Remote work is within reach of anywhere, so it never loses points for
    distance. Otherwise distance only counts once the account has a home.
    """
    if is_remote:
        return 10, "Remote"
    if profile.home is None or latitude is None or longitude is None:
        return 0, None
    miles = miles_between(profile.home[0], profile.home[1], latitude, longitude)
    radius = max(profile.radius_miles, 1)
    if miles <= radius:
        return 15, f"{miles:.0f} mi from home"
    if miles <= radius * 2:
        return 3, f"{miles:.0f} mi from home, a little past your {radius} mi radius"
    return -15, f"{miles:.0f} mi from home and not remote"


def _skill_points(profile, payload):
    title = str(payload.get("title") or "")
    in_title = skills_mentioned(title, profile.skills)
    matched = skills_mentioned(posting_text(payload), profile.skills)

    points, reasons = 0, []
    if in_title:
        # The title is what the job *is*; the description is everything it
        # touches. A title hit is worth far more than any one mention.
        points += min(25 + 5 * (len(in_title) - 1), 35)
        reasons.append(f"Title matches your {', '.join(sorted(in_title)[:3])}")
    if matched:
        points += min(len(matched) * 5, 30)
        shown = ", ".join(matched[:4]) + ("…" if len(matched) > 4 else "")
        reasons.append(f"{len(matched)} of your skills appear: {shown}")
    return points, reasons, matched


def score_posting(payload, profile=BLANK_PROFILE, *, is_remote=None, latitude=None, longitude=None):
    """
    Returns a Fit for one posting against one account's profile.

    `is_remote` and the coordinates are the stored facets (see
    mappers.derive_facets); left as None, remoteness is read from the payload
    and distance is skipped.
    """
    if not isinstance(payload, dict):
        return Fit(score=0)

    if is_remote is None:
        is_remote = payload.get("isRemote") is True

    score = BASELINE
    reasons = []

    if profile.skills:
        points, skill_reasons, matched = _skill_points(profile, payload)
        score += points
        reasons.extend(skill_reasons)
    else:
        matched = []
        reasons.append("Add a résumé in Identity to rank jobs by your skills")

    for points, reason in (
        _location_points(profile, is_remote, latitude, longitude),
        _freshness_points(payload),
    ):
        score += points
        if reason:
            reasons.append(reason)

    title = str(payload.get("title") or "").lower()
    if any(word in title for word in UNPAID_TITLES):
        # Applied last: zeroing the running total mid-function doesn't work,
        # because every rule after it would add points back on.
        reasons.append("Unpaid or volunteer position")
        return Fit(score=0, reasons=reasons, matched_skills=matched)

    return Fit(score=max(0, min(MAX_SCORE, score)), reasons=reasons, matched_skills=matched)
