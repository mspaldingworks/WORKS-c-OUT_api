from django.urls import path
from rest_framework.routers import DefaultRouter

from .feed_views import SharedPostingFeedView
from .views import ApifyWebhookView, IngestedPostingViewSet, IngestView

router = DefaultRouter()
router.register("postings", IngestedPostingViewSet)

urlpatterns = [
    path("ingest/", IngestView.as_view(), name="ingest"),
    path("apify/", ApifyWebhookView.as_view(), name="apify-webhook"),
    # Read-only, key-authorised feed for partner apps to mirror. Listed
    # ahead of the router so "feed" is never read as a posting id.
    path("feed/", SharedPostingFeedView.as_view(), name="shared-posting-feed"),
] + router.urls
