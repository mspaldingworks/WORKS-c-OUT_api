import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.serializers import ApplicationSerializer

from identity.models import ProfessionalProfile

from .generation import GenerationUnavailable, generate_materials
from .mappers import fetch_dataset_items
from .models import IngestedPosting
from .serializers import IngestedPostingSerializer
from .services import ingest_items, promote_posting_to_application

logger = logging.getLogger(__name__)


def _has_valid_ingestion_key(request):
    """
    Shared secret for machine callers (Apify, n8n) that have no user session.
    Accepts a header or a query param: Apify's webhook UI doesn't expose custom
    headers on every plan tier, and the URL is the only field always available.
    """
    provided = request.headers.get("X-Ingestion-Key") or request.query_params.get("key", "")
    return bool(settings.INGESTION_API_KEY) and provided == settings.INGESTION_API_KEY


class IngestedPostingViewSet(viewsets.ModelViewSet):
    """Browsing/triage of ingested postings from the native app."""

    queryset = IngestedPosting.objects.all()
    serializer_class = IngestedPostingSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        # The Job Feed only ever wants new postings; filtering server-side keeps
        # it from pulling every posting ever scraped once the daily runs pile up.
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    @action(detail=True, methods=["post"])
    def materials(self, request, pk=None):
        """
        Tailored cover letter and resume for this posting. Cached after the
        first run so re-opening a posting is free; pass ?refresh=1 to redo it.
        """
        posting = self.get_object()
        if posting.generated_materials and request.query_params.get("refresh") != "1":
            return Response(posting.generated_materials)

        profile = ProfessionalProfile.objects.first()
        try:
            materials = generate_materials(posting, profile.master_resume if profile else "")
        except GenerationUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        posting.generated_materials = materials
        posting.save(update_fields=["generated_materials"])
        return Response(materials)

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        """Take a posting out of the feed. Reversible via restore."""
        posting = self.get_object()
        posting.status = IngestedPosting.Status.DISMISSED
        posting.save(update_fields=["status"])
        return Response(self.get_serializer(posting).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """Undo a dismissal — §3.5 of the app's own rules asks for undo, not a
        confirmation dialog, because dialogs get dismissed reflexively."""
        posting = self.get_object()
        posting.status = IngestedPosting.Status.NEW
        posting.save(update_fields=["status"])
        return Response(self.get_serializer(posting).data)

    @action(detail=True, methods=["post"])
    def promote(self, request, pk=None):
        posting = self.get_object()
        if posting.status == IngestedPosting.Status.TRIAGED:
            return Response(
                {"detail": "This posting was already promoted to an application."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        application = promote_posting_to_application(posting)
        return Response(ApplicationSerializer(application).data, status=status.HTTP_201_CREATED)


class IngestView(APIView):
    """
    Generic webhook target for external automation to push in scraped/RSS/email
    -sourced job postings. Accepts either a single posting object or a list.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not _has_valid_ingestion_key(request):
            return Response({"detail": "Invalid or missing ingestion key."}, status=status.HTTP_401_UNAUTHORIZED)

        many = isinstance(request.data, list)
        serializer = IngestedPostingSerializer(data=request.data, many=many)
        serializer.is_valid(raise_exception=True)
        try:
            # Savepoint, so a duplicate-key failure rolls back cleanly instead of
            # poisoning the surrounding transaction for anything that follows.
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            # Same (source, url) already stored — the caller re-sent something we
            # have. A clean 409 beats a 500, and beats silently duplicating.
            return Response(
                {"detail": "A posting with this source and url already exists."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ApifyWebhookView(APIView):
    """
    Target for Apify webhooks (one per board task, each carrying ?source=indeed
    etc. so the tasks self-identify).

    Apify's webhook payload contains run metadata only — never the scraped items
    — so this pulls the run's dataset and normalizes it here. Apify retries on
    any non-2xx, so "nothing to do" cases deliberately return 200.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not _has_valid_ingestion_key(request):
            return Response({"detail": "Invalid or missing ingestion key."}, status=status.HTTP_401_UNAUTHORIZED)

        payload = request.data if isinstance(request.data, dict) else {}

        event_type = payload.get("eventType", "")
        if event_type and event_type != "ACTOR.RUN.SUCCEEDED":
            # A failed/aborted run has nothing to ingest, but it isn't an error
            # on our side — 200 so Apify doesn't retry it.
            return Response({"detail": f"Ignored event {event_type}.", "created": 0})

        dataset_id = (payload.get("resource") or {}).get("defaultDatasetId")
        if not dataset_id:
            # Misconfigured webhook (wrong payload template) — surface it as a
            # failure in Apify's webhook dashboard rather than silently passing.
            return Response(
                {"detail": "Payload has no resource.defaultDatasetId."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        board = request.query_params.get("source", "unknown")
        source = f"apify:{board}"

        try:
            items = fetch_dataset_items(dataset_id, token=settings.APIFY_TOKEN)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            # Transient Apify/network problem — 502 so Apify retries later. Log
            # the real cause; a bare 502 with no detail is miserable to debug
            # (a missing APIFY_TOKEN shows up here as a 403, for instance).
            logger.exception("Failed to fetch Apify dataset %s", dataset_id)
            return Response(
                {"detail": "Couldn't fetch the Apify dataset."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(ingest_items(items, source=source))
