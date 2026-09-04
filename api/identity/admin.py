from django.contrib import admin

from .models import ProfessionalProfile, ProfileLink, ResumeVersion, Skill


@admin.register(ProfessionalProfile)
class ProfessionalProfileAdmin(admin.ModelAdmin):
    list_display = ("headline", "owner", "updated_at")


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "category", "proficiency")
    list_filter = ("category", "proficiency", "owner")


@admin.register(ProfileLink)
class ProfileLinkAdmin(admin.ModelAdmin):
    list_display = ("platform", "url", "status", "owner")
    list_filter = ("status", "owner")


@admin.register(ResumeVersion)
class ResumeVersionAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "created_at", "discarded_at")
    list_filter = ("owner",)
