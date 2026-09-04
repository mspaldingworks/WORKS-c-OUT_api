from rest_framework.routers import DefaultRouter

from .views import ApplicationEventViewSet, ApplicationViewSet, CompanyViewSet, ContactViewSet

router = DefaultRouter()
router.register("companies", CompanyViewSet)
router.register("contacts", ContactViewSet)
router.register("applications", ApplicationViewSet)
router.register("events", ApplicationEventViewSet)

urlpatterns = router.urls
