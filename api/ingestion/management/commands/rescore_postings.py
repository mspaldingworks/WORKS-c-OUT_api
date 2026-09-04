from django.core.management.base import BaseCommand

from ingestion.models import IngestedPosting
from ingestion.scoring import score_posting


class Command(BaseCommand):
    help = "Recompute fit scores for stored postings. Run after changing scoring.py."

    def handle(self, *args, **options):
        postings = list(IngestedPosting.objects.all())
        for posting in postings:
            posting.score, posting.score_reasons = score_posting(posting.raw_payload)
            if not posting.apply_url:
                apply_url = (posting.raw_payload or {}).get("applyUrl") or ""
                posting.apply_url = apply_url if len(apply_url) <= 1000 else ""
        IngestedPosting.objects.bulk_update(postings, ["score", "score_reasons", "apply_url"], batch_size=200)
        self.stdout.write(self.style.SUCCESS(f"Rescored {len(postings)} postings."))
