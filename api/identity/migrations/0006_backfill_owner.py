# Hand-written data migration — see ingestion/migrations/0005_dedupe_postings_by_url.py
# for the established pattern this follows (RunPython + noop reverse, historical
# models via apps.get_model, cross-app dependencies declared explicitly).
#
# WORKS(c)OUT moved from one implicit profile to real per-account ownership.
# This backfills the `owner` FK added (nullable) by the three AddField
# migrations this depends on, onto whichever account the app's existing data
# actually belongs to — then the following AlterField migrations make `owner`
# required now that every row has one.
import os

from django.contrib.auth.hashers import make_password
from django.db import migrations

OWNER_EMAIL = os.environ.get("WORKS_COUT_OWNER_EMAIL", "") or "mspaldingworks@gmail.com"

# Resolving the owner by "whoever already holds the app's existing token" is the
# least ambiguous option when migrating a database that already has real data —
# it doesn't assume a user id, a username, or that there's exactly one account.
# The token itself is a live credential and this repo is public, so it comes
# from the environment: set WORKS_COUT_EXISTING_TOKEN_KEY when migrating a
# database whose token predates this migration. Without it, the owner is matched
# by email, and a fresh/local/test database (which has neither) falls through to
# creating the account.
EXISTING_TOKEN_KEY = os.environ.get("WORKS_COUT_EXISTING_TOKEN_KEY", "")


def backfill_owner(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Token = apps.get_model("authtoken", "Token")
    ProfessionalProfile = apps.get_model("identity", "ProfessionalProfile")
    Skill = apps.get_model("identity", "Skill")
    ProfileLink = apps.get_model("identity", "ProfileLink")
    ResumeVersion = apps.get_model("identity", "ResumeVersion")
    IngestedPosting = apps.get_model("ingestion", "IngestedPosting")
    Application = apps.get_model("tracker", "Application")

    token = Token.objects.filter(key=EXISTING_TOKEN_KEY).first() if EXISTING_TOKEN_KEY else None
    if token:
        owner = token.user
    else:
        owner = User.objects.filter(email=OWNER_EMAIL).first()
        if owner is None:
            owner = User.objects.create(
                username="madelyn",
                email=OWNER_EMAIL,
                password=make_password(None),
                is_staff=True,
                is_superuser=True,
            )
        if EXISTING_TOKEN_KEY:
            Token.objects.get_or_create(key=EXISTING_TOKEN_KEY, defaults={"user_id": owner.pk})

    ProfessionalProfile.objects.filter(owner__isnull=True).update(owner_id=owner.pk)
    Skill.objects.filter(owner__isnull=True).update(owner_id=owner.pk)
    ProfileLink.objects.filter(owner__isnull=True).update(owner_id=owner.pk)
    ResumeVersion.objects.filter(owner__isnull=True).update(owner_id=owner.pk)
    IngestedPosting.objects.filter(owner__isnull=True).update(owner_id=owner.pk)
    Application.objects.filter(owner__isnull=True).update(owner_id=owner.pk)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0005_professionalprofile_owner_profilelink_owner_and_more"),
        ("ingestion", "0008_remove_ingestedposting_unique_posting_per_url_and_more"),
        ("tracker", "0006_application_owner"),
    ]

    operations = [
        migrations.RunPython(backfill_owner, noop),
    ]
