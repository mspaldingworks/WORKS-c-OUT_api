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
    class Meta:
        model = JobFilterPreferences
        fields = ["salary", "remote", "job_type", "match_score", "updated_at"]
        read_only_fields = ["updated_at"]


class LLMCredentialSerializer(serializers.ModelSerializer):
    # api_key is write-only and never serialized back; reads only ever see the
    # masked hint. required=False so PATCH can edit model/base_url without
    # re-sending the key — create() enforces its presence.
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=False)
    masked_key = serializers.CharField(read_only=True)

    class Meta:
        model = LLMCredential
        fields = ["id", "provider", "api_key", "masked_key", "model", "base_url", "is_active", "updated_at"]
        read_only_fields = ["is_active", "updated_at"]

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
        if not raw:
            raise serializers.ValidationError({"api_key": "An API key is required."})
        owner = validated_data.pop("owner")
        provider = validated_data["provider"]
        # Upsert on (owner, provider) so re-saving a provider updates it rather
        # than colliding with the unique constraint.
        cred, _ = LLMCredential.objects.get_or_create(
            owner=owner, provider=provider, defaults={"api_key_encrypted": ""},
        )
        cred.model = validated_data.get("model", cred.model)
        cred.base_url = validated_data.get("base_url", cred.base_url)
        cred.set_key(raw)
        # Saving a key makes it the active provider ("Save & use").
        LLMCredential.objects.filter(owner=owner).exclude(pk=cred.pk).update(is_active=False)
        cred.is_active = True
        cred.save()
        return cred

    def update(self, instance, validated_data):
        raw = validated_data.pop("api_key", None)
        for field in ("model", "base_url"):
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
        fields = ["id", "title", "file", "notes", "parsed_data", "created_at"]
        read_only_fields = ["parsed_data", "created_at"]

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
                    f"You can keep at most {MAX_RESUMES_PER_OWNER} résumé versions — "
                    "delete one before adding another."
                )
        return attrs
