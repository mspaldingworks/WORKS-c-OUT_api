from django.core.management.base import BaseCommand

from tracker.sheets import SheetUnavailable, sync_sheet


class Command(BaseCommand):
    help = "Rewrite the Google Sheet from current application records."

    def handle(self, *args, **options):
        try:
            count = sync_sheet()
        except SheetUnavailable as error:
            self.stderr.write(str(error))
            return
        self.stdout.write(self.style.SUCCESS(f"Synced {count} applications to the sheet."))
