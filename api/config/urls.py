from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/tracker/", include("tracker.urls")),
    path("api/identity/", include("identity.urls")),
    path("api/ingestion/", include("ingestion.urls")),
    # Member-facing pages. An account provisioned for a partner app's member
    # is reachable here with a one-time emailed link, so it is genuinely
    # theirs rather than only reachable through that app.
    path("account/", include("identity.account_urls")),
]
