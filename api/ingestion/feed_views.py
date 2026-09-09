"""
The shared posting feed for partner apps.

TransWell mirrors these postings into its own Jobs tab. It used to read
`/api/ingestion/postings/` authenticated as the owning account, because that
viewset filters on `owner=request.user` — which meant a partner app held a
person's own long-lived credential and could reach everything that account
owns: their résumés, their profile, their applications. Rotating that token
also broke the mirror, so the credential was effectively permanent.

This endpoint exists so the mirror needs none of that. It is authorised by its
own key, serves postings and nothing else, and is read-only.

It also strips the fields that describe one person's job search rather than
the job — score, score_reasons, generated materials, owner, triage status.
TransWell already dropped those on arrival, but dropping them here means they
never cross the wire in the first place, which is the difference between a
partner app choosing not to look and not being shown.
"""

import logging

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from identity.owners import NoDefaultOwner, get_default_owner

from .models import IngestedPosting
from .serializers import IngestedPostingSerializer

logger = logging.getLogger(__name__)

# Fields that describe one person's search, never the job. Kept here as the
# authoritative list; the mirror downstream has its own copy as a backstop,
# and the two agreeing is checked by a test.
PERSONAL_FIELDS = frozenset({
    'score',
    'score_reasons',
    'generated_materials',
    'owner',
    'status',
})

MAX_LIMIT = 500


def _has_valid_feed_key(request):
    provided = request.headers.get('X-Feed-Key', '')
    expected = getattr(settings, 'FEED_API_KEY', '')
    return bool(expected) and provided == expected


class FeedKeyOnly(BaseAuthentication):
    """No user is authenticated; the key alone authorises reading the feed.

    Declared so DRF doesn't fall through to TokenAuthentication and reject the
    request before the view runs.
    """

    def authenticate(self, request):
        return None


class SharedPostingFeedView(APIView):
    """GET /api/ingestion/feed/ — postings for a partner app to mirror.

    Read-only and key-authorised. Returns the same posting shape the app uses,
    minus anything personal.
    """

    authentication_classes = [FeedKeyOnly]
    permission_classes = [AllowAny]

    def get(self, request):
        if not _has_valid_feed_key(request):
            return Response({'detail': 'Invalid or missing feed key.'}, status=401)

        try:
            owner = get_default_owner()
        except NoDefaultOwner as error:
            logger.warning('Shared feed has no resolvable owner: %s', error)
            return Response({'detail': 'Feed owner is not configured.'}, status=503)

        try:
            limit = min(int(request.query_params.get('limit', MAX_LIMIT)), MAX_LIMIT)
        except (TypeError, ValueError):
            limit = MAX_LIMIT

        postings = (
            IngestedPosting.objects
            .filter(owner=owner)
            .order_by('-created_at')[:max(limit, 1)]
        )

        serializer = IngestedPostingSerializer(
            postings, many=True, context={'request': request},
        )
        results = [_without_personal_fields(row) for row in serializer.data]
        return Response({'count': len(results), 'results': results})


def _without_personal_fields(row):
    row = {k: v for k, v in row.items() if k not in PERSONAL_FIELDS}
    row['skills'] = _job_skills(row.get('skills'))
    return row


def _job_skills(raw):
    """The job's own skills, as a plain list.

    The app's shape is {matched, missing} — which of the job's skills that
    account has and lacks. That split describes a person, not a posting, and
    means nothing in a feed that belongs to no one, so it is flattened here.
    With no signed-in user there is nothing to match against, so what remains
    is what the posting itself asked for.
    """
    if isinstance(raw, dict):
        names = []
        for value in raw.values():
            if isinstance(value, (list, tuple)):
                names.extend(value)
            elif value:
                names.append(value)
    elif isinstance(raw, (list, tuple)):
        names = list(raw)
    else:
        return []

    seen, unique = set(), []
    for name in names:
        text = str(name).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            unique.append(text)
    return unique
