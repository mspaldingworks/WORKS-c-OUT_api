"""Member-facing pages, mounted at /account/ rather than under /api/."""

from django.urls import path

from . import account_views

urlpatterns = [
    path("", account_views.home, name="account-home"),
    path("sign-in/", account_views.sign_in, name="account-sign-in"),
    path("sign-in/<str:token>/", account_views.consume, name="account-consume"),
    path("sign-out/", account_views.sign_out, name="account-sign-out"),
    path(
        "documents/<int:resume_id>/delete/",
        account_views.delete_resume,
        name="account-delete-resume",
    ),
    path("delete/", account_views.delete_account, name="account-delete"),
]
