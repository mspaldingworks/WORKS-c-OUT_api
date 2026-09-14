from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import JobFilterPreferences, ProfessionalProfile, ProfileLink, ResumeVersion, Skill
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
