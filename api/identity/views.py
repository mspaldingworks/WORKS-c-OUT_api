from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.response import Response

from .models import JobFilterPreferences, ProfessionalProfile, ProfileLink, ResumeVersion, Skill
from .resume_parsing import ParsingUnavailable, parse_resume_text
from .resume_text import TextExtractionFailed, extract_resume_text
from .serializers import (
    JobFilterPreferencesSerializer,
    ProfessionalProfileSerializer,
    ProfileLinkSerializer,
    ResumeVersionSerializer,
    SkillSerializer,
)


class OwnerScopedViewSet(viewsets.ModelViewSet):
    """Every row belongs to whoever created it; no account can see another's."""

    def get_queryset(self):
        return super().get_queryset().filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class ProfessionalProfileViewSet(OwnerScopedViewSet):
    queryset = ProfessionalProfile.objects.all()
    serializer_class = ProfessionalProfileSerializer

    def perform_create(self, serializer):
        # owner is a OneToOneField — one profile per account. A second POST
        # from the same account should read as "you already have one", not a
        # bare 500 from the database constraint.
        try:
            serializer.save(owner=self.request.user)
        except IntegrityError:
            raise ValidationError({"detail": "A professional profile already exists for this account."})


class SkillViewSet(OwnerScopedViewSet):
    queryset = Skill.objects.all()
    serializer_class = SkillSerializer


class JobFilterPreferencesView(RetrieveUpdateAPIView):
    """
    The account's chosen set of visible Job-Feed filters.

    GET auto-creates the row with defaults so the app always has something to
    render on first launch; PATCH toggles individual filters. A singleton per
    account, so there's no id in the URL — it's always "mine".
    """

    serializer_class = JobFilterPreferencesSerializer

    def get_object(self):
        preferences, _ = JobFilterPreferences.objects.get_or_create(owner=self.request.user)
        return preferences


class ProfileLinkViewSet(OwnerScopedViewSet):
    queryset = ProfileLink.objects.all()
    serializer_class = ProfileLinkSerializer


class ResumeVersionViewSet(OwnerScopedViewSet):
    queryset = ResumeVersion.objects.all()
    serializer_class = ResumeVersionSerializer

    def get_queryset(self):
        # Discarded résumés stay in the database so undo can put them back, but
        # they're gone as far as the app is concerned.
        return super().get_queryset().filter(discarded_at__isnull=True)

    @action(detail=True, methods=["post"])
    def discard(self, request, pk=None):
        """
        Remove a résumé from the list. Reversible via restore — §3.5 asks for
        undo rather than a confirmation dialog, so this must not be a delete.
        """
        resume = self.get_object()
        resume.discarded_at = timezone.now()
        resume.save(update_fields=["discarded_at"])
        return Response(self.get_serializer(resume).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """
        Undo a discard, putting the résumé and its parsed suggestions back.

        Looks past get_queryset's filter on purpose — the whole point is to
        reach a row the list no longer shows — but stays scoped to the caller.
        """
        resume = get_object_or_404(ResumeVersion, pk=pk, owner=request.user)
        resume.discarded_at = None
        resume.save(update_fields=["discarded_at"])
        return Response(self.get_serializer(resume).data)

    @action(detail=True, methods=["post"])
    def parse(self, request, pk=None):
        """
        Extract skill/profile suggestions from this résumé. Cached after the
        first run so re-opening it is free; pass ?refresh=1 to redo it.
        """
        resume = self.get_object()
        if resume.parsed_data and request.query_params.get("refresh") != "1":
            return Response(resume.parsed_data)

        try:
            text = extract_resume_text(resume.file)
        except TextExtractionFailed as error:
            return Response({"detail": str(error)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        try:
            parsed = parse_resume_text(text)
        except ParsingUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        resume.parsed_data = parsed
        resume.save(update_fields=["parsed_data"])
        return Response(parsed)
