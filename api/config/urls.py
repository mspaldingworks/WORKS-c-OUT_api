from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/tracker/", include("tracker.urls")),
    path("api/identity/", include("identity.urls")),
    path("api/ingestion/", include("ingestion.urls")),
]
