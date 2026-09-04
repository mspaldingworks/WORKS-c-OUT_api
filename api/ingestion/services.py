from tracker.models import Application, Company

from .mappers import normalize_item
from .models import IngestedPosting


def ingest_items(items, source):
    """
    Normalize a batch of scraped items and save the ones we haven't seen before.

    Deduped on (source, url), both against what's already stored and within the
    batch itself — a scraper re-run returns mostly the same jobs, and the user
    shouldn't have to re-triage them.

    Returns counts: created / duplicates / unmappable.
    """
    normalized = [mapped for mapped in (normalize_item(item, source) for item in items) if mapped]
    unmappable = len(items) - len(normalized)

    # Deliberately NOT filtered by source: the six per-lane searches overlap, so
    # the same job arrives from several of them. Scoping this to one source is
    # what produced duplicate postings — and duplicate applications behind them.
    batch_urls = {mapped["url"] for mapped in normalized if mapped["url"]}
    already_stored = set(
        IngestedPosting.objects.filter(url__in=batch_urls).values_list("url", flat=True)
    )

    to_create = []
    seen_in_batch = set()
    for mapped in normalized:
        url = mapped["url"]
        if url:
            if url in already_stored or url in seen_in_batch:
                continue
            seen_in_batch.add(url)
        to_create.append(IngestedPosting(**mapped))

    # ignore_conflicts covers the race where two runs finish at once and both
    # pass the check above; the partial unique index is the real guarantee.
    IngestedPosting.objects.bulk_create(to_create, ignore_conflicts=True)

    return {
        "created": len(to_create),
        "duplicates": len(normalized) - len(to_create),
        "unmappable": unmappable,
    }


def promote_posting_to_application(posting: IngestedPosting) -> Application:
    """Create a tracker Application from an ingested posting and mark it triaged."""
    company_name = posting.company_name or "Unknown"
    company, _ = Company.objects.get_or_create(name=company_name)
    application = Application.objects.create(
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
