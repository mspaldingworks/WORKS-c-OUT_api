"""
The member-facing web surface.

Accounts provisioned for a partner app's members (see `partner_views`) have no
usable password and are otherwise reachable only through that app's token.
These pages are what make such an account genuinely the member's: they can
sign in with a one-time emailed link, see what is on it, delete a document, or
delete the account outright — without going through the partner app.

Server-rendered and dependency-free on purpose. This is an escape hatch and a
right of access, not a second product.
"""

import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .magic_links import MagicLinkToken, redeem, request_link_for_email
from .models import ResumeVersion, Skill
from .partner_views import USERNAME_PREFIX

logger = logging.getLogger(__name__)

LOGIN_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _minutes():
    return int(MagicLinkToken.LIFETIME.total_seconds() // 60)


@require_http_methods(["GET", "POST"])
def sign_in(request):
    """Ask for a sign-in link."""
    if request.user.is_authenticated:
        return redirect("account-home")

    if request.method == "POST":
        # Sent for a real address, silently dropped for one with no account —
        # the page says the same thing either way, so this can't be used to
        # find out who has an account here.
        request_link_for_email(request.POST.get("email", ""))
        return render(request, "identity/link_sent.html", {"minutes": _minutes()})

    return render(request, "identity/sign_in.html")


@require_http_methods(["GET"])
def consume(request, token):
    """Redeem a link and start a session."""
    user = redeem(token)
    if user is None:
        return render(
            request, "identity/link_invalid.html", {"minutes": _minutes()},
        )

    login(request, user, backend=LOGIN_BACKEND)
    logger.info("Magic-link sign-in for %s", user.username)
    return redirect("account-home")


@login_required(login_url="/account/sign-in/")
@require_http_methods(["GET"])
def home(request):
    """Everything on this account, and the two ways to take it back."""
    resumes = list(
        ResumeVersion.objects.filter(
            owner=request.user, discarded_at__isnull=True,
        )
    )
    for resume in resumes:
        resume.skill_count = len((resume.parsed_data or {}).get("skills") or [])

    return render(request, "identity/account.html", {
        "resumes": resumes,
        "skills": Skill.objects.filter(owner=request.user).order_by("name"),
        "connected_partner": request.user.username.startswith(f"{USERNAME_PREFIX}-"),
    })


@login_required(login_url="/account/sign-in/")
@require_http_methods(["POST"])
def delete_resume(request, resume_id):
    resume = ResumeVersion.objects.filter(
        pk=resume_id, owner=request.user,
    ).first()
    if resume is None:
        messages.info(request, "That document is already gone.")
        return redirect("account-home")

    # A real deletion, not the discard the app's undo uses: someone who came
    # here to remove a document means remove it.
    resume.file.delete(save=False)
    resume.delete()
    messages.info(request, "Document deleted.")
    return redirect("account-home")


@login_required(login_url="/account/sign-in/")
@require_http_methods(["POST"])
def delete_account(request):
    user = request.user
    logger.info("Account self-deleted: %s", user.username)
    logout(request)
    user.delete()
    return render(request, "identity/base.html", {
        "messages": ["Your account and everything on it has been deleted."],
    })


@require_http_methods(["POST"])
def sign_out(request):
    logout(request)
    return redirect(reverse("account-sign-in"))
