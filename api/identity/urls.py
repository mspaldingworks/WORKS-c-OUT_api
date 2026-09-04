from rest_framework.routers import DefaultRouter

from .views import ProfessionalProfileViewSet, ProfileLinkViewSet, ResumeVersionViewSet, SkillViewSet

router = DefaultRouter()
router.register("profile", ProfessionalProfileViewSet)
router.register("skills", SkillViewSet)
router.register("links", ProfileLinkViewSet)
router.register("resumes", ResumeVersionViewSet)

urlpatterns = router.urls
