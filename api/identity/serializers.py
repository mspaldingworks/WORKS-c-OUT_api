from rest_framework import serializers

from .models import ProfessionalProfile, ProfileLink, ResumeVersion, Skill


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


class ProfileLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProfileLink
        fields = ["id", "platform", "url", "status", "notes"]


class ResumeVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResumeVersion
        fields = ["id", "title", "file", "notes", "created_at"]
