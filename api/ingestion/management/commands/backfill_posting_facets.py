from django.core.management.base import BaseCommand

from ingestion.mappers import derive_facets
from ingestion.models import IngestedPosting


class Command(BaseCommand):
    help = (
        "Recompute salary/remote/job-type facets for stored postings from their "
        "raw_payload. Run after changing salary.py or the facet derivation."
    )

    def handle(self, *args, **options):
        postings = list(IngestedPosting.objects.all())
        for posting in postings:
            facets = derive_facets(posting.raw_payload or {})
            posting.salary_min_annual = facets["salary_min_annual"]
            posting.salary_max_annual = facets["salary_max_annual"]
            posting.is_remote = facets["is_remote"]
            posting.employment_types = facets["employment_types"]
        IngestedPosting.objects.bulk_update(
            postings,
            ["salary_min_annual", "salary_max_annual", "is_remote", "employment_types"],
            batch_size=200,
        )
        self.stdout.write(self.style.SUCCESS(f"Backfilled facets for {len(postings)} postings."))
