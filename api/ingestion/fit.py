"""
Per-account job feeds: who receives postings, and keeping each account's
scores in step with its own profile.

Every account that signs in to WORKS(c)OUT directly gets its own copy of each
scraped posting. A copy, rather than one shared row, because everything the
app does to a posting is personal — dismissing it, promoting it to an
application, the cover letter written for it, its fit score — and those all
live on the row. Partner-provisioned accounts (TransWell members) are left
out: they read the shared feed through their partner app, and there can be
far more of them.

Copies scale with accounts × postings. That is fine for a handful of accounts;
well before hundreds, postings should become one shared table with a
per-account state table beside it.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction

from identity.models import JobFilterPreferences, Skill
from identity.owners import NoDefaultOwner, get_default_owner
from identity.partner_views import USERNAME_PREFIX

from .models import IngestedPosting
from .scoring import ScoringProfile, score_posting

logger = logging.getLogger(__name__)

# How many of the owner's current postings a brand-new account starts with.
# Matches the shared partner feed's cap.
SEED_LIMIT = 500

# Columns copied when one account's posting seeds another's feed. Personal
# state (status, score, generated materials) is deliberately not among them.
COPIED_FIELDS = [
    "source", "title", "company_name", "url", "apply_url", "raw_payload",
    "salary_min_annual", "salary_max_annual", "is_remote", "employment_types",
    "work_arrangement", "latitude", "longitude", "posted_at",
]


def feed_recipients():
    """Every account whose feed receives newly scraped postings."""
    return list(
        get_user_model().objects.filter(is_active=True)
        .exclude(username__startswith=f"{USERNAME_PREFIX}-")
        .order_by("pk")
    )


def profile_for(owner):
    preferences = JobFilterPreferences.objects.filter(owner=owner).first()
    return ScoringProfile(
        skills=tuple(Skill.objects.filter(owner=owner).values_list("name", flat=True)),
        home=preferences.home if preferences else None,
        radius_miles=preferences.radius_miles if preferences else 25,
    )


def apply_fit(posting, profile):
    """Score one (possibly unsaved) posting for its owner, in place."""
    payload = posting.raw_payload if isinstance(posting.raw_payload, dict) else {}
    if posting.title and not payload.get("title"):
        # Boards name the field differently (positionName, jobTitle, …); the
        # mapped title is the one every board agrees on.
        payload = {**payload, "title": posting.title}
    fit = score_posting(
        payload, profile,
        is_remote=posting.is_remote or posting.work_arrangement == "remote",
        latitude=posting.latitude, longitude=posting.longitude,
    )
    posting.score = fit.score
    posting.score_reasons = fit.reasons
    posting.matched_skills = fit.matched_skills
    return posting


def rescore_owner(owner, profile=None):
    """
    Recompute this account's feed against its profile.

    Only postings still in the feed: one already dismissed, applied to or
    retired isn't ranked anywhere, and restoring one rescores it then.
    """
    profile = profile or profile_for(owner)
    queryset = (
        IngestedPosting.objects.filter(owner=owner, status=IngestedPosting.Status.NEW)
        .only("pk", "title", "raw_payload", "is_remote", "work_arrangement", "latitude", "longitude")
        .order_by("pk")
    )
    batch, count = [], 0
    # Chunked: the web container shares a small memory limit, and every
    # payload carries a full job description.
    for posting in queryset.iterator(chunk_size=200):
        batch.append(apply_fit(posting, profile))
        if len(batch) >= 200:
            IngestedPosting.objects.bulk_update(batch, ["score", "score_reasons", "matched_skills"])
            count += len(batch)
            batch = []
    if batch:
        IngestedPosting.objects.bulk_update(batch, ["score", "score_reasons", "matched_skills"])
        count += len(batch)
    JobFilterPreferences.objects.update_or_create(
        owner=owner, defaults={"scoring_signature": profile.signature()},
    )
    return count


def refresh_if_stale(owner):
    """
    Rescore this account's postings if its skills, home or radius changed
    since they were last scored. Run when the feed loads, so adding twenty
    skills one at a time costs one rescore, not twenty.
    """
    profile = profile_for(owner)
    preferences = JobFilterPreferences.objects.filter(owner=owner).first()
    if preferences and preferences.scoring_signature == profile.signature():
        return 0
    return rescore_owner(owner, profile)


def seed_feed(owner):
    """
    Give an account with no postings at all a copy of the owner's current
    feed, scored for this account. Returns how many were copied.

    Runs on that account's first feed load. The owner's own account is never
    seeded from itself, and an account that has postings is left alone — an
    empty feed after dismissing everything is a real state, not a new account.
    """
    try:
        source_owner = get_default_owner()
    except NoDefaultOwner:
        return 0
    if source_owner.pk == owner.pk or IngestedPosting.objects.filter(owner=owner).exists():
        return 0

    profile = profile_for(owner)
    source = (
        IngestedPosting.objects.filter(owner=source_owner, status=IngestedPosting.Status.NEW)
        .only(*COPIED_FIELDS)
        .order_by("-created_at")[:SEED_LIMIT]
    )
    copies = []
    for posting in source.iterator(chunk_size=200):
        copy = IngestedPosting(owner=owner, **{name: getattr(posting, name) for name in COPIED_FIELDS})
        copies.append(apply_fit(copy, profile))

    with transaction.atomic():
        IngestedPosting.objects.bulk_create(copies, batch_size=200, ignore_conflicts=True)
        JobFilterPreferences.objects.update_or_create(
            owner=owner, defaults={"scoring_signature": profile.signature()},
        )
    logger.info("Seeded %s postings for account %s", len(copies), owner.pk)
    return len(copies)
