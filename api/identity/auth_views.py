"""
Signing up and signing in from the app itself.

Until now the only ways to hold an account were to be the owner (whose token
is compiled into the app) or to be provisioned by a partner app (see
`partner_views`). These endpoints are the third way: a person makes an account
with an email and password and gets the same kind of token back, so the app
can be signed out of one account and into another.

The properties worth keeping:

* A new account starts empty. Nothing is copied from the owner's account — no
  profile, skills, résumés or filter choices — so what a new person sees is
  the product's defaults and nothing of anyone else's.
* A new account brings its own AI key. Anyone can sign up, so the server's
  key is not offered to accounts made here (see `llm.SELF_MADE_GROUP`).
* Signing out is the app forgetting its token. The token is not deleted here:
  an account has one token shared by every device it is signed in on, and the
  owner's is compiled into her builds, so revoking on sign-out would sign out
  her other device and strand those builds.
* A failed sign-in says the same thing whether the address is unknown or the
  password is wrong.
* Both endpoints are throttled per address (see settings), since neither can
  ask for a credential first.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .llm import SELF_MADE_GROUP, uses_server_key
from .partner_views import USERNAME_PREFIX

logger = logging.getLogger(__name__)

SIGN_IN_FAILED = "That email and password don't match an account."


def _credentials(request):
    email = str(request.data.get("email") or "").strip()
    password = str(request.data.get("password") or "")
    errors = {}
    if not email:
        errors["email"] = ["Enter your email address."]
    if not password:
        errors["password"] = ["Enter a password."]
    if errors:
        raise ValidationError(errors)
    return email, password


def _session(user):
    token, _ = Token.objects.get_or_create(user=user)
    return {"token": token.key, **_account(user)}


def _account(user):
    return {
        "email": user.email,
        "username": user.username,
        # So the app can say up front that drafts and résumé reading need a
        # key added, instead of leaving it to the first failed attempt.
        "uses_server_ai_key": uses_server_key(user),
    }


def _direct_accounts():
    """Accounts a person can sign in to here. Partner-provisioned ones are
    reached through the partner app or an emailed link, never a password."""
    return get_user_model().objects.exclude(username__startswith=f"{USERNAME_PREFIX}-")


class SignUpView(APIView):
    """POST /api/identity/auth/sign-up/ — create an account and sign in to it.

    Body: {"email": "...", "password": "..."}

    The email doubles as the username. It is not verified: nothing here sends
    mail, so a typo makes an account nobody else can reach, not a leak.
    """

    # No authentication at all, rather than AllowAny alone: with the default
    # classes a stale or empty Authorization header is rejected with a 401
    # before the view runs, and a signed-out app has nothing valid to send.
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sign-up"

    def post(self, request):
        email, password = _credentials(request)
        User = get_user_model()
        username = email.lower()

        try:
            validate_email(email)
        except DjangoValidationError:
            raise ValidationError({"email": ["Enter a valid email address."]})
        if len(username) > User._meta.get_field("username").max_length:
            raise ValidationError({"email": ["That email address is too long."]})
        if username.startswith(f"{USERNAME_PREFIX}-"):
            # That prefix is how partner-provisioned accounts are told apart,
            # and the partner key may delete anything carrying it.
            raise ValidationError({"email": ["That email address can't be used here."]})

        taken = _direct_accounts().filter(
            Q(username__iexact=username) | Q(email__iexact=email)
        ).exists()
        if taken:
            raise ValidationError({"email": ["An account with this email already exists. Sign in instead."]})

        try:
            validate_password(password, user=User(username=username, email=email))
        except DjangoValidationError as error:
            raise ValidationError({"password": list(error.messages)})

        try:
            with transaction.atomic():
                user = User.objects.create_user(username=username, email=email, password=password)
                group, _ = Group.objects.get_or_create(name=SELF_MADE_GROUP)
                user.groups.add(group)
        except IntegrityError:
            # Two sign-ups for one address at the same moment.
            raise ValidationError({"email": ["An account with this email already exists. Sign in instead."]})

        logger.info("Account signed up: %s", user.pk)
        return Response(_session(user), status=status.HTTP_201_CREATED)


class SignInView(APIView):
    """POST /api/identity/auth/sign-in/ — exchange an email and password for
    the account's token.

    Body: {"email": "...", "password": "..."}
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sign-in"

    def post(self, request):
        email, password = _credentials(request)

        # By username or by the email on the account: an account made here has
        # the two equal, but the owner's predates sign-up and may not.
        matches = list(
            _direct_accounts().filter(Q(username__iexact=email) | Q(email__iexact=email))[:2]
        )
        user = matches[0] if len(matches) == 1 else None

        if user is None:
            # Hash anyway, so an unknown address takes as long as a wrong
            # password and the two can't be told apart by timing.
            get_user_model()().set_password(password)
        elif user.is_active and user.check_password(password):
            return Response(_session(user))

        raise ValidationError({"non_field_errors": [SIGN_IN_FAILED]})


class AccountView(APIView):
    """GET /api/identity/auth/me/ — which account this token belongs to."""

    def get(self, request):
        return Response(_account(request.user))
