from django.core.management.base import BaseCommand

from ingestion.details import payload_of
from ingestion.fit import rescore_owner
from ingestion.models import IngestedPosting


class Command(BaseCommand):
    help = "Recompute fit scores for every account's postings. Run after changing scoring.py."

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        owners = get_user_model().objects.filter(
            pk__in=IngestedPosting.objects.values("owner").distinct()
        )
        total = 0
        for owner in owners:
            total += rescore_owner(owner)

        # Older rows predate apply_url being stored separately.
        missing = IngestedPosting.objects.filter(apply_url="").only("pk", "raw_payload", "apply_url")
        for posting in missing.iterator(chunk_size=200):
            apply_url = payload_of(posting).get("applyUrl") or ""
            if apply_url and len(apply_url) <= 1000:
                posting.apply_url = apply_url
                posting.save(update_fields=["apply_url"])
        self.stdout.write(self.style.SUCCESS(f"Rescored {total} postings across {owners.count()} accounts."))
