from pathlib import Path

from django.core.management.base import BaseCommand

from identity.models import ProfessionalProfile

DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "data" / "master_resume.md"


class Command(BaseCommand):
    help = "Load the master background that tailored application materials are written from."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_SOURCE))

    def handle(self, *args, **options):
        source = Path(options["file"])
        if not source.exists():
            self.stderr.write(f"No such file: {source}")
            return

        text = source.read_text().strip()
        # The materials endpoint reads master_resume off the first profile, so
        # write to that one; a fresh deploy has no profile row at all.
        profile = ProfessionalProfile.objects.first()
        created = profile is None
        if created:
            profile = ProfessionalProfile(headline="Director of Development")
        profile.master_resume = text
        profile.save()

        verb = "Created profile and loaded" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} master resume ({len(text)} chars) from {source.name}"))
