import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.serializers import ApplicationSerializer

from identity.llm import resolve_config
from identity.models import ProfessionalProfile
from identity.owners import NoDefaultOwner, get_default_owner

from .generation import GenerationUnavailable, generate_materials
from .mappers import fetch_dataset_items
from .models import IngestedPosting
from .serializers import IngestedPostingSerializer
from .services import ingest_items, promote_posting_to_application

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _as_int(value):
    """Parse a query-param int, or None — garbage in a filter is ignored rather
    than 500ing the whole feed."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_true(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in _TRUE_VALUES


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
        queryset = super().get_queryset().filter(owner=self.request.user)
        params = self.request.query_params

        # The Job Feed only ever wants new postings; filtering server-side keeps
        # it from pulling every posting ever scraped once the daily runs pile up.
        status_filter = params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        queryset = self._filter_by_salary(queryset, params)

        # Which of the optional facet filters are surfaced in the app is the
        # user's choice (see identity.JobFilterPreferences); the server honors
        # whichever params actually arrive.
        if _is_true(params.get("remote")):
            queryset = queryset.filter(is_remote=True)

        job_types = [token for token in params.getlist("job_type") if token]
        if job_types:
            match = Q()
            for token in job_types:
                match |= Q(employment_types__contains=[token])
            queryset = queryset.filter(match)

        min_score = _as_int(params.get("min_score"))
        if min_score is not None:
            queryset = queryset.filter(score__gte=min_score)

        return queryset

    @staticmethod
    def _filter_by_salary(queryset, params):
        """
        Keep postings whose advertised annual band overlaps the requested one.

        Postings with no listed pay have null bounds and are included by default
        — a salary filter that silently dropped every unpriced job would gut the
        feed — unless the caller passes include_unspecified_salary=0.
        """
        salary_min = _as_int(params.get("salary_min"))
        salary_max = _as_int(params.get("salary_max"))
        if salary_min is None and salary_max is None:
            return queryset

        overlaps = Q()
        if salary_min is not None:
            overlaps &= Q(salary_max_annual__gte=salary_min)
        if salary_max is not None:
            overlaps &= Q(salary_min_annual__lte=salary_max)

        if _is_true(params.get("include_unspecified_salary"), default=True):
            overlaps |= Q(salary_min_annual__isnull=True, salary_max_annual__isnull=True)

        return queryset.filter(overlaps)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def materials(self, request, pk=None):
        """
        Tailored cover letter and resume for this posting. Cached after the
        first run so re-opening a posting is free; pass ?refresh=1 to redo it.
        """
        posting = self.get_object()
        if posting.generated_materials and request.query_params.get("refresh") != "1":
            return Response(posting.generated_materials)

        profile = ProfessionalProfile.objects.filter(owner=request.user).first()
        try:
            materials = generate_materials(
                posting,
                profile.master_resume if profile else "",
                config=resolve_config(request.user),
            )
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

        try:
            owner = get_default_owner()
        except NoDefaultOwner as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        many = isinstance(request.data, list)
        serializer = IngestedPostingSerializer(data=request.data, many=many)
        serializer.is_valid(raise_exception=True)
        try:
            # Savepoint, so a duplicate-key failure rolls back cleanly instead of
            # poisoning the surrounding transaction for anything that follows.
            with transaction.atomic():
                serializer.save(owner=owner)
        except IntegrityError:
            # Same (owner, url) already stored — the caller re-sent something we
            # have. A clean 409 beats a 500, and beats silently duplicating.
            return Response(
                {"detail": "A posting with this url already exists."},
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

        try:
            owner = get_default_owner()
        except NoDefaultOwner as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

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

        return Response(ingest_items(items, source=source, owner=owner))
