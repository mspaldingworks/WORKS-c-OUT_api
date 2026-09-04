from rest_framework import viewsets

from .models import ProfessionalProfile, ProfileLink, ResumeVersion, Skill
from .serializers import (
    ProfessionalProfileSerializer,
    ProfileLinkSerializer,
    ResumeVersionSerializer,
    SkillSerializer,
)


class ProfessionalProfileViewSet(viewsets.ModelViewSet):
    queryset = ProfessionalProfile.objects.all()
    serializer_class = ProfessionalProfileSerializer


class SkillViewSet(viewsets.ModelViewSet):
    queryset = Skill.objects.all()
    serializer_class = SkillSerializer


class ProfileLinkViewSet(viewsets.ModelViewSet):
    queryset = ProfileLink.objects.all()
    serializer_class = ProfileLinkSerializer


class ResumeVersionViewSet(viewsets.ModelViewSet):
    queryset = ResumeVersion.objects.all()
    serializer_class = ResumeVersionSerializer
