import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from . import google_oauth
from .models import (
    GoogleDriveConnection,
    JobFilterPreferences,
    LLMCredential,
    ProfessionalProfile,
    ProfileLink,
    ResumeVersion,
    Skill,
)
from .llm import resolve_config

logger = logging.getLogger(__name__)
from .resume_parsing import ParsingUnavailable, parse_resume_text
from .resume_text import TextExtractionFailed, extract_resume_text
from .serializers import (
    JobFilterPreferencesSerializer,
    LLMCredentialSerializer,
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


class LLMCredentialViewSet(OwnerScopedViewSet):
    """CRUD for an account's own AI provider keys. The key is write-only and
    only ever comes back masked (see the serializer)."""

    queryset = LLMCredential.objects.all()
    serializer_class = LLMCredentialSerializer

    def destroy(self, request, *args, **kwargs):
        # Return the deleted row (200) instead of an empty 204, so the app's
        # decoder always has a body to read.
        instance = self.get_object()
        data = self.get_serializer(instance).data
        instance.delete()
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Make this the provider actually used, clearing any other active one."""
        cred = self.get_object()
        LLMCredential.objects.filter(owner=request.user).exclude(pk=cred.pk).update(is_active=False)
        cred.is_active = True
        cred.save(update_fields=["is_active"])
        return Response(self.get_serializer(cred).data)


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


def _drive_status(connection):
    if connection is None:
        return {"connected": False, "enabled": False, "account_email": "", "folder_id": ""}
    return {
        "connected": connection.connected,
        "enabled": connection.enabled,
        "account_email": connection.account_email,
        "folder_id": connection.folder_id,
    }


class DriveConnectionView(APIView):
    """GET this account's Google Drive status; PATCH the on/off toggle."""

    def get(self, request):
        connection = GoogleDriveConnection.objects.filter(owner=request.user).first()
        return Response(_drive_status(connection))

    def patch(self, request):
        connection = GoogleDriveConnection.objects.filter(owner=request.user).first()
        if connection is None or not connection.connected:
            return Response({"detail": "Connect Google Drive before changing this."},
                            status=status.HTTP_400_BAD_REQUEST)
        if "enabled" in request.data:
            connection.enabled = bool(request.data["enabled"])
            connection.save(update_fields=["enabled", "updated_at"])
        return Response(_drive_status(connection))


class DriveConnectView(APIView):
    """Return the Google consent URL the app opens to authorize their Drive."""

    def get(self, request):
        if not google_oauth.client_configured():
            return Response({"detail": "Google Drive isn't configured on the server yet."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"auth_url": google_oauth.auth_url(request.user.pk)})


class DriveCallbackView(APIView):
    """
    Where Google redirects after consent. There's no app auth header on this
    browser hop — the user is carried in the signed `state`. Always bounces back
    to the app's return scheme so the in-app auth session closes either way.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        if request.query_params.get("error"):
            return self._return(ok=False, reason=request.query_params["error"])
        try:
            user_id = google_oauth.read_state(request.query_params.get("state"))
            payload = google_oauth.exchange_code(request.query_params.get("code"))
        except google_oauth.OAuthError as error:
            return self._return(ok=False, reason=str(error))

        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            return self._return(ok=False, reason="Account not found.")

        refresh_token = payload["refresh_token"]
        email = google_oauth.fetch_email(payload.get("access_token", ""))

        # Create the app's folder in their Drive up front, so uploads have a home.
        from tracker.drive import credentials_for, ensure_folder

        folder_id = ""
        try:
            folder_id = ensure_folder(credentials_for(refresh_token))
        except Exception:
            logger.exception("Couldn't create the Drive folder for user %s", user_id)

        connection, _ = GoogleDriveConnection.objects.get_or_create(owner=user)
        connection.set_token(refresh_token)
        connection.account_email = email
        if folder_id:
            connection.folder_id = folder_id
        connection.enabled = True
        connection.save()
        return self._return(ok=True)

    def _return(self, ok, reason=""):
        from urllib.parse import quote

        url = f"{settings.GOOGLE_OAUTH_RETURN_URL}?ok={'1' if ok else '0'}"
        if reason:
            url += f"&reason={quote(reason)}"
        # Build the 302 by hand: HttpResponseRedirect rejects non-http(s) schemes
        # (DisallowedRedirect → 400), and the return target is a custom app scheme
        # (workscout://) the in-app auth session watches for.
        response = HttpResponse(status=302)
        response["Location"] = url
        return response


class DriveDisconnectView(APIView):
    """Forget this account's Drive connection. Uploads stop until reconnected."""

    def post(self, request):
        GoogleDriveConnection.objects.filter(owner=request.user).delete()
        return Response(_drive_status(None))


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
            parsed = parse_resume_text(text, config=resolve_config(request.user))
        except ParsingUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        resume.parsed_data = parsed
        resume.save(update_fields=["parsed_data"])
        return Response(parsed)
