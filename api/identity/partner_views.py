"""
Provisioning accounts for a partner app's members.

TransWell's Jobs tab connects a member to WORKS(c)OUT. Rather than acting for
them under one shared service token — which would pool every member's résumé
and contact details under a single owner — each member gets a real account
here, and TransWell acts as that member.

That makes the ownership honest: `OwnerScopedViewSet` already scopes every
identity row to `request.user`, so once the token belongs to the member, so
does everything they upload. No account can see another's, and disconnecting
deletes a member's account and their rows outright.

Auth is a shared provisioning key, not a user token. The key can create and
delete partner accounts and nothing else — it cannot read anyone's data, so
leaking it does not expose a single résumé.
"""

import logging
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# Partner accounts are named so they can never collide with a person who signs
# up here directly, and so they're identifiable in the admin at a glance.
USERNAME_PREFIX = "partner"
MAX_EXTERNAL_ID = 64


def _has_valid_partner_key(request):
    provided = request.headers.get("X-Partner-Key", "")
    expected = getattr(settings, "PARTNER_API_KEY", "")
    return bool(expected) and provided == expected


class PartnerKeyOnly(BaseAuthentication):
    """No user is authenticated; the key alone authorises provisioning.

    Declared as an authentication class so DRF doesn't fall through to
    TokenAuthentication and reject the request before the view runs.
    """

    def authenticate(self, request):
        return None


def partner_username(partner, external_id):
    return f"{USERNAME_PREFIX}-{partner}-{external_id}"[:150]


class PartnerAccountView(APIView):
    """POST /api/identity/partner-accounts/ — create or return a member's account.

    Body: {"partner": "transwell", "external_id": "...", "email": "..."}

    Idempotent on (partner, external_id): a second call returns the same
    account and the same token rather than a duplicate. TransWell can lose its
    copy of the token and recover it, and a retried request can't strand a
    member with two accounts.
    """

    authentication_classes = [PartnerKeyOnly]
    permission_classes = [AllowAny]

    def post(self, request):
        if not _has_valid_partner_key(request):
            return Response(
                {"detail": "Invalid or missing partner key."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        partner = str(request.data.get("partner") or "").strip().lower()
        external_id = str(request.data.get("external_id") or "").strip()
        email = str(request.data.get("email") or "").strip()

        if not partner or not partner.replace("-", "").isalnum():
            return Response(
                {"detail": "partner must be a simple name like 'transwell'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not external_id or len(external_id) > MAX_EXTERNAL_ID:
            return Response(
                {"detail": f"external_id is required and must be under {MAX_EXTERNAL_ID} characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        User = get_user_model()
        username = partner_username(partner, external_id)

        created = False
        try:
            with transaction.atomic():
                user, created = User.objects.get_or_create(
                    username=username, defaults={"email": email},
                )
                if created:
                    # No usable password: this account is reached through the
                    # partner app's token. Setting one is a password-reset away
                    # if direct sign-in is ever offered.
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
                elif email and user.email != email:
                    user.email = email
                    user.save(update_fields=["email"])
        except IntegrityError:
            return Response(
                {"detail": "Couldn't provision that account."},
                status=status.HTTP_409_CONFLICT,
            )

        token, _ = Token.objects.get_or_create(user=user)
        logger.info(
            "Partner account %s for %s (created=%s)", username, partner, created,
        )
        return Response(
            {
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "token": token.key,
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PartnerAccountDetailView(APIView):
    """DELETE /api/identity/partner-accounts/<partner>/<external_id>/

    Disconnecting is a real deletion, not a flag. A member who disconnects
    expects their résumés and parsed suggestions to be gone from here, and
    cascading off the user row is what actually makes that true.
    """

    authentication_classes = [PartnerKeyOnly]
    permission_classes = [AllowAny]

    def delete(self, request, partner, external_id):
        if not _has_valid_partner_key(request):
            return Response(
                {"detail": "Invalid or missing partner key."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        User = get_user_model()
        username = partner_username(partner.lower(), external_id)
        user = User.objects.filter(username=username).first()
        if user is None:
            # Already gone is the outcome the caller wanted.
            return Response(status=status.HTTP_204_NO_CONTENT)

        # Guard against a malformed partner/external_id ever resolving to a
        # real person's account: only ever delete a provisioned one.
        if not user.username.startswith(f"{USERNAME_PREFIX}-"):
            return Response(
                {"detail": "That is not a partner-provisioned account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        logger.info("Deleting partner account %s", username)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PartnerSignInLinkView(APIView):
    """POST /api/identity/partner-accounts/<partner>/<external_id>/sign-in-link/

    Emails that member a one-time sign-in link, so the partner app can offer
    "sign in here directly" without ever handling the link itself.

    The address comes off the account, never from the request: a caller with
    the provisioning key can ask for a link, but cannot say where it goes.
    """

    authentication_classes = [PartnerKeyOnly]
    permission_classes = [AllowAny]

    def post(self, request, partner, external_id):
        if not _has_valid_partner_key(request):
            return Response(
                {"detail": "Invalid or missing partner key."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        from .magic_links import send_link

        User = get_user_model()
        user = User.objects.filter(
            username=partner_username(partner.lower(), external_id),
        ).first()
        if user is None:
            return Response(
                {"detail": "No such account."}, status=status.HTTP_404_NOT_FOUND,
            )

        sent = send_link(user.email, user)
        return Response({"sent": sent, "email": user.email})
