from rest_framework import serializers

from .models import Application, ApplicationEvent, Company, Contact


class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "company", "name", "role", "email", "phone", "notes"]


class CompanySerializer(serializers.ModelSerializer):
    contacts = ContactSerializer(many=True, read_only=True)

    class Meta:
        model = Company
        fields = ["id", "name", "website", "notes", "created_at", "contacts"]


class ApplicationEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApplicationEvent
        fields = ["id", "application", "note", "occurred_at"]
        read_only_fields = ["occurred_at"]


class ApplicationSerializer(serializers.ModelSerializer):
    events = ApplicationEventSerializer(many=True, read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    # Surfaced so the queue screen and the auto-filler can reach the portal and
    # the tailored text without a second round trip per application.
    apply_url = serializers.SerializerMethodField()
    generated_materials = serializers.SerializerMethodField()
    platform = serializers.SerializerMethodField()

    def get_apply_url(self, application):
        posting = application.source_posting
        if not posting:
            return application.job_url
        return posting.apply_url or posting.url or application.job_url

    def get_platform(self, application):
        from ingestion.ats import describe

        return describe(self.get_apply_url(application))

    def get_generated_materials(self, application):
        # null, not {}, when there's nothing: an empty object claims to be
        # materials while missing every field, which is harder for a typed
        # client to handle than an honest absence.
        posting = application.source_posting
        return (posting.generated_materials if posting else None) or None

    class Meta:
        model = Application
        fields = [
            "id", "company", "company_name", "source_posting", "apply_url",
            "generated_materials", "platform", "role_title", "job_url", "status", "source",
            "applied_date", "salary_notes", "resume", "cover_letter",
            "resume_drive_url", "cover_letter_drive_url", "notes",
            "created_at", "updated_at", "events",
        ]
