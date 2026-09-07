"""
Issuing and redeeming one-time sign-in links.

The security properties worth stating, because each is easy to lose in a
refactor:

* Only a hash of the token is stored. A database copy must not hand anyone a
  working link — the plaintext exists once, in the email.
* A link is single-use and short-lived. Redeeming marks it used in the same
  breath as signing in, so a link forwarded or left in an inbox is spent.
* Requesting a link never reveals whether an account exists. The answer is the
  same either way, so this can't be used to test whether someone has one.
* Requesting a link cannot redirect it. The email always goes to the address
  on the account, never to an address in the request.
"""

import hashlib
import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import MagicLinkToken

logger = logging.getLogger(__name__)

# Enough entropy that guessing is not a strategy, short enough to survive an
# email client wrapping the URL.
TOKEN_BYTES = 32

# How many unused links one account may have outstanding. Asking again should
# work — people do — but not accumulate valid links forever.
MAX_OUTSTANDING = 5


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(user):
    """Creates a link for `user` and returns the plaintext token.

    The caller emails it. Nothing else should ever see it.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with transaction.atomic():
        # Older unused links stay valid — a member who asks twice and clicks
        # the first email should not be told it's broken — but they're capped.
        outstanding = MagicLinkToken.objects.filter(
            user=user, used_at__isnull=True, expires_at__gt=timezone.now(),
        ).order_by("-created_at")
        for stale in outstanding[MAX_OUTSTANDING - 1:]:
            stale.delete()

        MagicLinkToken.objects.create(
            user=user,
            token_hash=hash_token(token),
            expires_at=timezone.now() + MagicLinkToken.LIFETIME,
        )
    return token


def redeem(token):
    """Returns the user this token signs in, or None.

    Marks the link used before returning, so a token that gets replayed —
    a forwarded email, a browser prefetch, a back button — finds it spent.
    """
    if not token:
        return None

    with transaction.atomic():
        link = (
            MagicLinkToken.objects
            .select_for_update()
            .filter(token_hash=hash_token(token))
            .select_related("user")
            .first()
        )
        if link is None or not link.is_usable:
            return None
        link.used_at = timezone.now()
        link.save(update_fields=["used_at"])
        return link.user


def base_url():
    return (getattr(settings, "ACCOUNT_BASE_URL", "") or "").rstrip("/")


def sign_in_url(token):
    return f"{base_url()}/account/sign-in/{token}/"


SUBJECT = "Your WORKS(c)OUT sign-in link"

BODY = """Here is your sign-in link for WORKS(c)OUT:

{url}

It works once and expires in {minutes} minutes.

This account was created for you when you connected the Jobs tab in TransWell.
Signing in here lets you see and delete the documents and skills on it without
going through TransWell.

If you didn't ask for this link, you can ignore this email — nothing has
changed on your account.
"""


def send_link(email, user):
    """Emails a fresh link. Returns whether the send went out.

    `email` is read from the account, never from the request — a request can
    ask for a link, but it can't say where the link goes.
    """
    if not email:
        logger.info("No email on %s, so no link can be sent", user)
        return False

    token = issue(user)
    minutes = int(MagicLinkToken.LIFETIME.total_seconds() // 60)
    try:
        send_mail(
            SUBJECT,
            BODY.format(url=sign_in_url(token), minutes=minutes),
            settings.DEFAULT_FROM_EMAIL or None,
            [email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Couldn't send a sign-in link")
        return False
    return True


def request_link_for_email(email):
    """Sends a link to `email` if an account has it. Always reports nothing.

    The caller gets no signal either way on purpose: whether an address has an
    account here is not something an unauthenticated request should be able to
    find out.
    """
    user = get_user_model().objects.filter(email__iexact=(email or "").strip()).first()
    if user is None:
        logger.info("Sign-in link requested for an address with no account")
        return
    send_link(user.email, user)
