from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .ats import describe
from .details import describe as describe_details
from .skills import summarise as summarise_skills
from .models import IngestedPosting


class IngestedPostingSerializer(serializers.ModelSerializer):
    # Which ATS this applies through, and whether it will demand an account
    # before showing the form — the feed flags those so she isn't sent to a
    # wall from her phone.
    platform = serializers.SerializerMethodField()
    requires_account = serializers.SerializerMethodField()
    sign_in_url = serializers.SerializerMethodField()
    details = serializers.SerializerMethodField()
    skills = serializers.SerializerMethodField()

    def get_details(self, posting):
        return describe_details(posting)

    def get_skills(self, posting):
        return summarise_skills(posting, self._skill_names())

    def _skill_names(self):
        """
        Read her skill list once per response, not once per posting.

        Cached on the serializer instance: a 59-posting feed would otherwise
        issue 59 identical queries.
        """
        if not hasattr(self, "_cached_skill_names"):
            from identity.models import Skill

            self._cached_skill_names = tuple(Skill.objects.values_list("name", flat=True))
        return self._cached_skill_names

    def _ats(self, posting):
        return describe(posting.apply_url or posting.url)

    def get_platform(self, posting):
        return self._ats(posting)["platform"]

    def get_requires_account(self, posting):
        return self._ats(posting)["requires_account"]

    def get_sign_in_url(self, posting):
        return self._ats(posting)["sign_in_url"]

    class Meta:
        model = IngestedPosting
        # raw_payload is deliberately absent from the read shape: it made the
        # list response 710KB of scraper internals the app never decoded.
        # `details` carries the readable parts instead.
        fields = ["id", "source", "title", "company_name", "url", "apply_url", "raw_payload",
                  "status", "score", "score_reasons", "created_at",
                  "platform", "requires_account", "sign_in_url", "details", "skills"]
        extra_kwargs = {"raw_payload": {"write_only": True}}
        read_only_fields = ["status", "created_at", "score", "score_reasons"]
        # Meta-level validators only; the url field also gets its own
        # UniqueValidator from the model constraint — cleared in __init__ below.
        validators = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model's partial UniqueConstraint on url makes DRF attach a
        # UniqueValidator to the field, which does a lookup and rejects a repeat
        # with 400 before it ever reaches the database. That breaks two things:
        # email-sourced postings legitimately have no url and would all collide,
        # and callers expect a 409 for "already have this one", which the view
        # produces from the IntegrityError. Deduping stays the DB's job.
        self.fields["url"].validators = [
            v for v in self.fields["url"].validators
            if not isinstance(v, UniqueValidator)
        ]
