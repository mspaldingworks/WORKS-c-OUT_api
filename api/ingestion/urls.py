from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ApifyWebhookView, IngestedPostingViewSet, IngestView

router = DefaultRouter()
router.register("postings", IngestedPostingViewSet)

urlpatterns = [
    path("ingest/", IngestView.as_view(), name="ingest"),
    path("apify/", ApifyWebhookView.as_view(), name="apify-webhook"),
] + router.urls
