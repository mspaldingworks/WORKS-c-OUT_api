from django.urls import path
from rest_framework.routers import DefaultRouter

from .partner_views import PartnerAccountDetailView, PartnerAccountView
from .review_views import DocumentReviewView
from .views import ProfessionalProfileViewSet, ProfileLinkViewSet, ResumeVersionViewSet, SkillViewSet

router = DefaultRouter()
router.register("profile", ProfessionalProfileViewSet)
router.register("skills", SkillViewSet)
router.register("links", ProfileLinkViewSet)
router.register("resumes", ResumeVersionViewSet)

urlpatterns = [
    # Stateless — parses and answers without creating a row. Listed ahead of
    # the router so it can never be read as a detail route.
    path("review-document/", DocumentReviewView.as_view(), name="review-document"),
    # Provisioning accounts for a partner app's members, so each member owns
    # their own rows here instead of sharing one service account.
    path("partner-accounts/", PartnerAccountView.as_view(), name="partner-accounts"),
    path(
        "partner-accounts/<str:partner>/<str:external_id>/",
        PartnerAccountDetailView.as_view(),
        name="partner-account-detail",
    ),
] + router.urls
