import time

from django.core.management.base import BaseCommand

from ingestion.freshness import sweep
from ingestion.models import IngestedPosting


class Command(BaseCommand):
    help = "Retire postings whose listing the employer has taken down."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="check at most this many")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="seconds between requests, to stay polite")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        queryset = IngestedPosting.objects.filter(
            status=IngestedPosting.Status.NEW
        ).order_by("-score")
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        postings = list(queryset)
        self.stdout.write(f"Checking {len(postings)} postings…")

        def report(posting, verdict, detail):
            symbol = {"gone": "✗", "live": "✓", "unknown": "?"}[verdict]
            self.stdout.write(f"  {symbol} {posting.title[:44]:<44} {detail[:44]}")
            time.sleep(options["delay"])

        if options["dry_run"]:
            # Report without writing: the same sweep against an empty list of
            # postings to save, so nothing is retired.
            from ingestion.freshness import check_url

            for posting in postings:
                verdict, detail = check_url(posting.apply_url or posting.url)
                report(posting, verdict, detail)
            return

        summary = sweep(postings, on_result=report)
        self.stdout.write(self.style.SUCCESS(
            f"\nChecked {summary['checked']}: {summary['retired']} retired, "
            f"{summary['live']} live, {summary['unknown']} couldn't be determined."
        ))
