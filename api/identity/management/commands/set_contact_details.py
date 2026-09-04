from django.core.management.base import BaseCommand

from identity.models import ProfessionalProfile, ProfileLink

FIELDS = ["legal_name", "email", "phone", "city_state", "street_address",
          "postal_code", "linkedin_url", "portfolio_url"]


class Command(BaseCommand):
    help = "Set the contact details the application auto-filler puts into employer forms."

    def add_arguments(self, parser):
        for field in FIELDS:
            parser.add_argument(f"--{field.replace('_', '-')}")

    def handle(self, *args, **options):
        profile = ProfessionalProfile.objects.first() or ProfessionalProfile()

        for field in FIELDS:
            value = options.get(field)
            if value:
                setattr(profile, field, value)

        # The LinkedIn URL is already recorded as a ProfileLink; default to it
        # rather than making her type the same URL into a second place.
        if not profile.linkedin_url:
            link = ProfileLink.objects.filter(platform__iexact="LinkedIn").first()
            if link:
                profile.linkedin_url = link.url

        profile.save()

        self.stdout.write(self.style.SUCCESS("Contact details saved:"))
        for field in FIELDS:
            value = getattr(profile, field) or self.style.WARNING("(not set)")
            self.stdout.write(f"  {field}: {value}")
