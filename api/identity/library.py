"""
The cover-letter library, and where an account keeps its documents.

The library is every cover letter in one list: the ones a person uploaded and
the ones WORKS(c)OUT wrote for a job. It's a view over what already exists —
uploaded letters are ResumeVersion rows, written ones live on the posting they
were written for and the application made from it — so nothing is copied and
an edit made while reviewing a draft shows up here as it is.
"""

from datetime import datetime

from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .document_storage import stored_in
from .models import DocumentSettings, GoogleDriveConnection, ResumeVersion

SORTS = {"newest", "oldest", "company", "role"}
SOURCES = {"all", "generated", "uploaded"}
PREVIEW_CHARS = 600


def _drive_available(user):
    from tracker.drive import _is_default_owner

    connection = GoogleDriveConnection.objects.filter(owner=user).first()
    if connection and connection.connected:
        return True
    from django.conf import settings

    return _is_default_owner(user) and bool(settings.GOOGLE_OAUTH_REFRESH_TOKEN)


class DocumentSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentSettings
        fields = ["storage", "updated_at"]
        read_only_fields = ["updated_at"]


class DocumentSettingsView(APIView):
    """GET/PATCH {"storage": "both" | "app" | "drive"} — applies to documents
    saved from now on; nothing already saved is moved."""

    def get(self, request):
        row, _ = DocumentSettings.objects.get_or_create(owner=request.user)
        return Response({**DocumentSettingsSerializer(row).data,
                         "drive_available": _drive_available(request.user)})

    def patch(self, request):
        row, _ = DocumentSettings.objects.get_or_create(owner=request.user)
        serializer = DocumentSettingsSerializer(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        storage = serializer.validated_data.get("storage", row.storage)
        if storage != DocumentSettings.Storage.APP and not _drive_available(request.user):
            return Response({"storage": ["Connect Google Drive first to save documents there."]},
                            status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response({**serializer.data, "drive_available": _drive_available(request.user)})


def _when(value, *fallbacks):
    for candidate in (value, *fallbacks):
        if isinstance(candidate, datetime):
            return candidate
        if isinstance(candidate, str) and candidate:
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
    return timezone.now()


def _preview(text):
    return " ".join(str(text or "").split())[:PREVIEW_CHARS]


def _generated(request):
    from ingestion.models import IngestedPosting
    from tracker.models import Application

    postings = (
        IngestedPosting.objects.filter(owner=request.user, generated_materials__has_key="cover_letter")
        .only("pk", "title", "company_name", "generated_materials", "created_at")
    )
    applications = {
        app.source_posting_id: app
        for app in Application.objects.filter(owner=request.user, source_posting__in=postings)
        .select_related("company").order_by("created_at")
    }
    items = []
    for posting in postings:
        text = str(posting.generated_materials.get("cover_letter") or "").strip()
        if not text:
            continue
        app = applications.get(posting.pk)
        here = bool(app and app.cover_letter)
        drive_url = app.cover_letter_drive_url if app else ""
        items.append({
            "id": f"generated-{posting.pk}",
            "source": "generated",
            "title": app.role_title if app else posting.title,
            "company": (app.company.name if app else posting.company_name) or "",
            "created_at": _when(posting.generated_materials.get("generated_at"),
                                app.created_at if app else None, posting.created_at),
            "status": app.get_status_display() if app else "Not applied yet",
            "text": text,
            "preview": _preview(text),
            "file_url": request.build_absolute_uri(
                reverse("application-download", args=[app.pk, "cover-letter"])) if here else "",
            "drive_url": drive_url,
            "stored_in": "both" if here and drive_url else "drive" if drive_url else "app",
            "application_id": app.pk if app else None,
            "posting_id": posting.pk,
        })
    return items


def _uploaded(request):
    letters = ResumeVersion.objects.filter(
        owner=request.user, kind=ResumeVersion.Kind.COVER_LETTER, discarded_at__isnull=True,
    )
    return [{
        "id": f"uploaded-{letter.pk}",
        "source": "uploaded",
        "title": letter.title,
        "company": "",
        "created_at": letter.created_at,
        "status": "",
        # The full text isn't kept for an upload — the file is the letter.
        "text": "",
        "preview": (letter.parsed_data or {}).get("preview", ""),
        "file_url": request.build_absolute_uri(
            reverse("resumeversion-download", args=[letter.pk])) if letter.file else "",
        "drive_url": letter.drive_url,
        "stored_in": stored_in(letter),
        "document_id": letter.pk,
    } for letter in letters]


class CoverLetterLibraryView(APIView):
    """
    GET ?sort=newest|oldest|company|role&source=all|generated|uploaded

    Every cover letter this account has, written here or uploaded, newest
    first by default.
    """

    def get(self, request):
        sort = request.query_params.get("sort", "newest")
        source = request.query_params.get("source", "all")
        if sort not in SORTS or source not in SOURCES:
            return Response({"detail": "Unknown sort or source."}, status=status.HTTP_400_BAD_REQUEST)

        items = []
        if source in {"all", "generated"}:
            items += _generated(request)
        if source in {"all", "uploaded"}:
            items += _uploaded(request)

        if sort == "company":
            # Letters with no company (most uploads) after the named ones.
            items.sort(key=lambda item: (not item["company"], item["company"].lower(),
                                         -item["created_at"].timestamp()))
        elif sort == "role":
            items.sort(key=lambda item: (item["title"].lower(), -item["created_at"].timestamp()))
        else:
            items.sort(key=lambda item: item["created_at"], reverse=(sort == "newest"))

        for item in items:
            item["created_at"] = item["created_at"].isoformat()
        return Response(items)
