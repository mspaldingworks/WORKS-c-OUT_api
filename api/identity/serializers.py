import re

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import (
    JobFilterPreferences,
    LLMCredential,
    ProfessionalProfile,
    ProfileLink,
    ResumeVersion,
    Skill,
)
from .validators import MAX_RESUMES_PER_OWNER, validate_resume_file

WORKSPACE_ID = re.compile(r"wrkspc_[A-Za-z0-9]{6,58}")


class ProfessionalProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProfessionalProfile
        fields = [
            "id", "headline", "summary", "master_resume",
            "legal_name", "email", "phone", "city_state", "street_address", "postal_code",
            "linkedin_url", "portfolio_url", "updated_at",
        ]


class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = ["id", "name", "category", "proficiency"]


class JobFilterPreferencesSerializer(serializers.ModelSerializer):
    home_latitude = serializers.FloatField(allow_null=True, required=False, min_value=-90, max_value=90)
    home_longitude = serializers.FloatField(allow_null=True, required=False, min_value=-180, max_value=180)
    radius_miles = serializers.IntegerField(required=False, min_value=1, max_value=500)

    class Meta:
        model = JobFilterPreferences
        fields = ["salary", "remote", "distance", "posted_date", "job_type", "match_score",
                  "easy_apply", "home_label", "home_latitude", "home_longitude",
                  "radius_miles", "sort", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate(self, attrs):
        # Half a coordinate pair is worse than none: every distance would be
        # measured from somewhere on the equator or the prime meridian.
        lat = attrs.get("home_latitude", getattr(self.instance, "home_latitude", None))
        lng = attrs.get("home_longitude", getattr(self.instance, "home_longitude", None))
        if (lat is None) != (lng is None):
            raise serializers.ValidationError(
                "Home location needs both a latitude and a longitude, or neither."
            )
        if lat is None and "home_label" not in attrs and (
            "home_latitude" in attrs or "home_longitude" in attrs
        ):
            # Clearing the coordinates clears the name shown for them too.
            attrs["home_label"] = ""
        return attrs


class LLMCredentialSerializer(serializers.ModelSerializer):
    # api_key is write-only and never serialized back; reads only ever see the
    # masked hint. required=False so PATCH can edit model/base_url without
    # re-sending the key — create() enforces its presence.
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=False)
    masked_key = serializers.CharField(read_only=True)

    class Meta:
        model = LLMCredential
        fields = ["id", "provider", "api_key", "masked_key", "model", "base_url", "workspace_id",
                  "is_active", "updated_at"]
        read_only_fields = ["is_active", "updated_at"]

    def validate_workspace_id(self, value):
        value = (value or "").strip()
        if value and not WORKSPACE_ID.fullmatch(value):
            raise serializers.ValidationError(
                "A workspace ID starts with wrkspc_ — copy it from the Claude Console's Workspaces page."
            )
        return value

    def validate(self, attrs):
        provider = attrs.get("provider") or getattr(self.instance, "provider", None)
        if provider == LLMCredential.Provider.CUSTOM:
            base_url = attrs.get("base_url") or getattr(self.instance, "base_url", "")
            model = attrs.get("model") or getattr(self.instance, "model", "")
            errors = {}
            if not base_url:
                errors["base_url"] = "A base URL is required for a custom provider."
            if not model:
                errors["model"] = "A model name is required for a custom provider."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        raw = validated_data.pop("api_key", "")
        owner = validated_data.pop("owner")
        provider = validated_data["provider"]
        existing = LLMCredential.objects.filter(owner=owner, provider=provider).first()
        # A key is needed the first time. After that, saving without one keeps
        # the stored key and changes only the settings beside it — so adding a
        # workspace ID doesn't mean pasting the key in again.
        if not raw and not (existing and existing.api_key_encrypted):
            raise serializers.ValidationError({"api_key": "An API key is required."})
        # Upsert on (owner, provider) so re-saving a provider updates it rather
        # than colliding with the unique constraint.
        cred, _ = LLMCredential.objects.get_or_create(
            owner=owner, provider=provider, defaults={"api_key_encrypted": ""},
        )
        cred.model = validated_data.get("model", cred.model)
        cred.base_url = validated_data.get("base_url", cred.base_url)
        cred.workspace_id = validated_data.get("workspace_id", cred.workspace_id)
        if raw:
            cred.set_key(raw)
        # Saving a key makes it the active provider ("Save & use").
        LLMCredential.objects.filter(owner=owner).exclude(pk=cred.pk).update(is_active=False)
        cred.is_active = True
        cred.save()
        return cred

    def update(self, instance, validated_data):
        raw = validated_data.pop("api_key", None)
        for field in ("model", "base_url", "workspace_id"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        if raw:
            instance.set_key(raw)
        instance.save()
        return instance


class ProfileLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProfileLink
        fields = ["id", "platform", "url", "status", "notes"]


class ResumeVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResumeVersion
        fields = ["id", "kind", "title", "file", "notes", "parsed_data", "drive_url", "created_at"]
        read_only_fields = ["parsed_data", "drive_url", "created_at"]
        extra_kwargs = {"file": {"required": True, "allow_empty_file": False}}

    def to_representation(self, instance):
        from django.urls import reverse

        from .document_storage import stored_in

        data = super().to_representation(instance)
        # The token-authenticated download, never the /media/ path: that one
        # is public to anyone who can guess it. Blank when the file is kept
        # only in Drive.
        request = self.context.get("request")
        path = reverse("resumeversion-download", args=[instance.pk])
        data["file"] = (request.build_absolute_uri(path) if request else path) if instance.file else ""
        data["stored_in"] = stored_in(instance)
        return data

    def validate_file(self, value):
        try:
            validate_resume_file(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message)
        return value

    def validate(self, attrs):
        if self.instance is None:
            owner = self.context["request"].user
            # Discarded ones don't count — removing a résumé has to actually
            # free the slot, or the cap becomes a dead end she can't clear.
            active = ResumeVersion.objects.filter(owner=owner, discarded_at__isnull=True)
            if active.count() >= MAX_RESUMES_PER_OWNER:
                raise serializers.ValidationError(
                    f"You can keep at most {MAX_RESUMES_PER_OWNER} résumés and cover letters — "
                    "remove one before adding another."
                )
        return attrs
