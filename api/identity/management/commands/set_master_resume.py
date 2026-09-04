from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from identity.models import ProfessionalProfile
from identity.owners import NoDefaultOwner, get_default_owner

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

        try:
            owner = get_default_owner()
        except NoDefaultOwner as error:
            raise CommandError(str(error))

        text = source.read_text().strip()
        profile, created = ProfessionalProfile.objects.get_or_create(
            owner=owner, defaults={"headline": "Director of Development"}
        )
        profile.master_resume = text
        profile.save()

        verb = "Created profile and loaded" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} master resume ({len(text)} chars) from {source.name}"))
