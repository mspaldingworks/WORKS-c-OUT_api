from django.contrib import admin

from .models import IngestedPosting
from .services import promote_posting_to_application


@admin.action(description="Promote to tracker application")
def promote_to_application(modeladmin, request, queryset):
    created = 0
    for posting in queryset:
        promote_posting_to_application(posting)
        created += 1
    modeladmin.message_user(request, f"Created {created} application(s) from selected posting(s).")


@admin.register(IngestedPosting)
class IngestedPostingAdmin(admin.ModelAdmin):
    list_display = ("title", "company_name", "source", "status", "created_at")
    list_filter = ("status", "source")
    search_fields = ("title", "company_name")
    actions = [promote_to_application]
