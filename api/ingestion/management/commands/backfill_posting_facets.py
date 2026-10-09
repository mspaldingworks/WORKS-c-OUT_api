from django.core.management.base import BaseCommand

from ingestion.mappers import FACET_FIELDS, derive_facets
from ingestion.models import IngestedPosting


class Command(BaseCommand):
    help = (
        "Recompute the filter facets (salary, workplace, location, listing date, "
        "job type) for stored postings from their "
        "raw_payload. Run after changing salary.py or the facet derivation."
    )

    def handle(self, *args, **options):
        postings = list(IngestedPosting.objects.all())
        for posting in postings:
            facets = derive_facets(posting.raw_payload or {}, posting.created_at.date())
            for field in FACET_FIELDS:
                setattr(posting, field, facets[field])
        IngestedPosting.objects.bulk_update(postings, FACET_FIELDS, batch_size=200)
        self.stdout.write(self.style.SUCCESS(f"Backfilled facets for {len(postings)} postings."))
