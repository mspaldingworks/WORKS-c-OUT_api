import datetime
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Coalesce, Lower, TruncDate
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.serializers import ApplicationSerializer

from identity.llm import resolve_config
from identity.models import JobFilterPreferences, ProfessionalProfile
from identity.owners import NoDefaultOwner, get_default_owner

from .ats import ACCOUNT_GATED_HOSTS
from .generation import GenerationUnavailable, generate_materials
from .geo import miles_from
from .mappers import fetch_dataset_items
from .models import IngestedPosting
from .placement import HYBRID, ONSITE, REMOTE
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


def _as_float(value):
    try:
        return float(value)
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
        workplaces = {token for token in params.getlist("workplace") if token in {REMOTE, HYBRID, ONSITE}}
        if _is_true(params.get("remote")):
            # The original remote-only toggle, kept for older clients.
            workplaces.add(REMOTE)
        if workplaces:
            match = Q(work_arrangement__in=workplaces)
            if REMOTE in workplaces:
                # is_remote and work_arrangement agree for anything ingested or
                # backfilled; honoring both keeps a row set by hand findable.
                match |= Q(is_remote=True)
            queryset = queryset.filter(match)

        job_types = [token for token in params.getlist("job_type") if token]
        if job_types:
            match = Q()
            for token in job_types:
                match |= Q(employment_types__contains=[token])
            queryset = queryset.filter(match)

        min_score = _as_int(params.get("min_score"))
        if min_score is not None:
            queryset = queryset.filter(score__gte=min_score)

        queryset = self._filter_by_search(queryset, params.get("q", ""))
        queryset = self._filter_by_posted_within(queryset, params)

        if _is_true(params.get("no_account")):
            queryset = queryset.exclude(self._account_gated())

        queryset = self._with_commute(queryset, params)
        return self._sorted(queryset, params.get("sort"))

    def _home(self, params):
        """
        Where distances are measured from: explicit near_lat/near_lng on the
        request, else the home saved in her filter preferences, else nowhere.
        """
        lat, lng = _as_float(params.get("near_lat")), _as_float(params.get("near_lng"))
        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
        if not hasattr(self, "_preferences"):
            self._preferences = JobFilterPreferences.objects.filter(owner=self.request.user).first()
        return self._preferences.home if self._preferences else None

    def _with_commute(self, queryset, params):
        """
        Annotate commute_miles — straight-line miles from home — and apply the
        within_miles radius.

        A fully remote job has no commute, so its distance is null rather than
        however far away the employer's office happens to be. The radius keeps
        remote jobs by default (they're within reach of anywhere) unless the
        caller passes include_remote=0; a posting with no known location can't
        be shown to be within range and is left out.
        """
        home = self._home(params)
        if home is None:
            return queryset

        queryset = queryset.annotate(
            commute_miles=Case(
                When(work_arrangement=REMOTE, then=Value(None)),
                When(latitude__isnull=True, then=Value(None)),
                When(longitude__isnull=True, then=Value(None)),
                default=miles_from(*home),
            )
        )

        radius = _as_float(params.get("within_miles"))
        if radius is not None and radius > 0:
            in_range = Q(commute_miles__lte=radius)
            if _is_true(params.get("include_remote"), default=True):
                in_range |= Q(work_arrangement=REMOTE)
            queryset = queryset.filter(in_range)
        return queryset

    @staticmethod
    def _filter_by_search(queryset, query):
        """
        Every word has to appear somewhere in the title, company or description —
        "grant writer louisville" narrows, rather than widens, as she types.
        """
        for term in query.split()[:8]:
            queryset = queryset.filter(
                Q(title__icontains=term)
                | Q(company_name__icontains=term)
                | Q(raw_payload__descriptionText__icontains=term)
            )
        return queryset

    @staticmethod
    def _filter_by_posted_within(queryset, params):
        """
        Listed in the last N days. The employer's own listing date is used where
        the board gave one; otherwise the day it was scraped, which is the
        latest it can have been listed.
        """
        days = _as_int(params.get("posted_within"))
        if days is None or days < 0:
            return queryset
        since = datetime.date.today() - datetime.timedelta(days=days)
        return queryset.annotate(
            listed_on=Coalesce("posted_at", TruncDate("created_at"))
        ).filter(listed_on__gte=since)

    @staticmethod
    def _account_gated():
        """
        Postings whose portal wants an account before it shows the form — the
        same hosts ats.describe() flags, matched on the link Apply would use.
        """
        gated = Q()
        for host in ACCOUNT_GATED_HOSTS:
            gated |= Q(apply_url__icontains=host) | Q(apply_url="", url__icontains=host)
        return gated

    @staticmethod
    def _sorted(queryset, sort):
        """
        best (default): fit score, newest first among ties.
        newest: employer's listing date, falling back to when it was scraped.
        pay: highest advertised top of band; unpriced jobs last.
        closest: shortest commute, remote and unlocated jobs last. Needs a home
            location; without one it falls back to best match.
        company: alphabetical by employer.
        """
        if sort == "newest":
            return queryset.annotate(
                sort_date=Coalesce("posted_at", TruncDate("created_at"))
            ).order_by("-sort_date", "-created_at", "-score")
        if sort == "pay":
            return queryset.order_by(
                F("salary_max_annual").desc(nulls_last=True),
                F("salary_min_annual").desc(nulls_last=True),
                "-score",
            )
        if sort == "closest" and "commute_miles" in queryset.query.annotations:
            return queryset.order_by(F("commute_miles").asc(nulls_last=True), "-score")
        if sort == "company":
            return queryset.order_by(Lower("company_name"), "-score")
        return queryset.order_by("-score", "-created_at")

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
