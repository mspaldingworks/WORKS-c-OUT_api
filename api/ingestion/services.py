from tracker.models import Application, Company

from .mappers import normalize_item
from .models import IngestedPosting


def ingest_items(items, source, owner=None, owners=None):
    """
    Normalize a batch of scraped items and save the ones each account hasn't
    seen before.

    `owners` is every account whose feed receives the batch (usually
    fit.feed_recipients()); `owner` alone is the one-account form. Each gets
    its own copy, scored against its own profile.

    Deduped on (owner, url), both against what's already stored and within the
    batch itself — a scraper re-run returns mostly the same jobs, and nobody
    should have to re-triage them.

    Returns counts summed across accounts: created / duplicates / unmappable.
    """
    from .fit import apply_fit, profile_for

    recipients = list(owners) if owners is not None else [owner]
    normalized = [mapped for mapped in (normalize_item(item, source) for item in items) if mapped]
    unmappable = len(items) - len(normalized)
    batch_urls = {mapped["url"] for mapped in normalized if mapped["url"]}

    created = duplicates = 0
    for recipient in recipients:
        # Deliberately NOT filtered by source: the per-search Apify tasks
        # overlap, so the same job arrives from several of them. Scoping this
        # to one source is what once produced duplicate postings — and
        # duplicate applications behind them.
        already_stored = set(
            IngestedPosting.objects.filter(owner=recipient, url__in=batch_urls).values_list("url", flat=True)
        )
        profile = profile_for(recipient)
        to_create = []
        seen_in_batch = set()
        for mapped in normalized:
            url = mapped["url"]
            if url:
                if url in already_stored or url in seen_in_batch:
                    continue
                seen_in_batch.add(url)
            to_create.append(apply_fit(IngestedPosting(owner=recipient, **mapped), profile))

        # ignore_conflicts covers the race where two runs finish at once and
        # both pass the check above; the partial unique index is the real guarantee.
        IngestedPosting.objects.bulk_create(to_create, ignore_conflicts=True)
        created += len(to_create)
        duplicates += len(normalized) - len(to_create)

    return {
        "created": created,
        "duplicates": duplicates,
        "unmappable": unmappable,
    }


def promote_posting_to_application(posting: IngestedPosting) -> Application:
    """Create a tracker Application from an ingested posting and mark it triaged."""
    company_name = posting.company_name or "Unknown"
    company, _ = Company.objects.get_or_create(name=company_name)
    application = Application.objects.create(
        owner=posting.owner,
        company=company,
        source_posting=posting,
        role_title=posting.title,
        job_url=posting.url,
        source=Application.Source.INGESTED,
        notes=f"Promoted from ingested posting (source: {posting.source}).",
    )
    posting.status = IngestedPosting.Status.TRIAGED
    posting.save(update_fields=["status"])
    return application
