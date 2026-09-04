from django.contrib import admin

from .models import Application, ApplicationEvent, Company, Contact


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 0


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "website")
    search_fields = ("name",)
    inlines = [ContactInline]


class ApplicationEventInline(admin.TabularInline):
    model = ApplicationEvent
    extra = 0
    readonly_fields = ("occurred_at",)


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("role_title", "company", "status", "source", "applied_date", "updated_at")
    list_filter = ("status", "source")
    search_fields = ("role_title", "company__name")
    inlines = [ApplicationEventInline]


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "role", "email")
    search_fields = ("name", "company__name")
