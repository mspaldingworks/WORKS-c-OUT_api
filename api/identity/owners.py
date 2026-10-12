"""
Resolves which account WORKS(c)OUT's data belongs to.

Every user-scoped row now carries a real `owner` FK, even though there is, in
practice, exactly one account today. An authenticated request always has
`request.user` to use directly. Machine callers with no request at all — the
ingestion webhook, the background prepare job when it needs to re-derive an
owner — call this instead.
"""

from django.conf import settings
from django.contrib.auth import get_user_model


class NoDefaultOwner(Exception):
    """Raised when no owner can be resolved for an unauthenticated caller."""


def get_default_owner():
    User = get_user_model()
    email = settings.WORKS_COUT_OWNER_EMAIL
    if email:
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            raise NoDefaultOwner(f"WORKS_COUT_OWNER_EMAIL is set to {email!r} but no such user exists.")
    try:
        return User.objects.get()
    except User.DoesNotExist:
        pass
    except User.MultipleObjectsReturned:
        # Anyone can now make an account (see auth_views), so "the only user"
        # stops holding the first time someone signs up. The one superuser is
        # still unambiguous, and keeps ingestion and the shared feed alive on
        # a server where the email was never set.
        try:
            return User.objects.get(is_superuser=True)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            pass
    raise NoDefaultOwner(
        "Can't resolve a default owner — set WORKS_COUT_OWNER_EMAIL or ensure exactly one user exists."
    )
