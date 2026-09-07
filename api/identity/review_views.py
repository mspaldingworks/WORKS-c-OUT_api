"""
Stateless document review for other apps on this account's stack.

TransWell members upload résumés in its Jobs tab and want them read by the
same AI parser the Identity tab uses. What it must NOT mean is those documents
landing here: every row in `identity` is owner-scoped, TransWell authenticates
with one shared service token, and `ProfessionalProfile` holds legal name,
address, phone and email. Storing other apps' members under that single owner
would pool strangers' résumés and contact details in one inbox — so this
endpoint reads the file, parses it, answers, and keeps nothing. No model is
touched; the only trace is the log line every request leaves.

The calling app stores the document and the extracted skills against its own
member's account, which is where they belong.
"""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .document_intake import (
    ALLOWED_EXTENSIONS,
    MAX_DOCUMENT_BYTES,
    DocumentUnreadable,
    extract_document_text,
)
from .resume_parsing import ParsingUnavailable, parse_resume_text

logger = logging.getLogger(__name__)


class DocumentReviewView(APIView):
    """POST /api/identity/review-document/ — parse a document, store nothing.

    Send either a multipart `file`, or JSON `{"text": "..."}` when the caller
    has already extracted the text itself.

    Returns the same shape `resume_parsing` produces — skills, headline,
    email, phone, linkedin_url — plus `characters`, so a caller can tell a
    thin extraction from a thin résumé.
    """

    def post(self, request):
        upload = request.FILES.get("file")
        if upload is not None:
            # .read() rather than chunks(): the size cap is well inside memory
            # and every extractor below needs the whole file anyway.
            try:
                text = extract_document_text(upload.read(), upload.name)
            except DocumentUnreadable as error:
                return Response(
                    {"detail": str(error)},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
        else:
            text = str(request.data.get("text") or "").strip()
            if not text:
                return Response(
                    {
                        "detail": "Send a `file` or a `text` body.",
                        "accepted_extensions": list(ALLOWED_EXTENSIONS),
                        "max_bytes": MAX_DOCUMENT_BYTES,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            parsed = parse_resume_text(text)
        except ParsingUnavailable as error:
            # 503 rather than 500: nothing about the document is wrong, the
            # parser just isn't answering, and the caller should retry.
            return Response(
                {"detail": str(error)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        logger.info(
            "Reviewed a document for %s (%d characters), stored nothing",
            request.user, len(text),
        )
        return Response({**parsed, "characters": len(text), "stored": False})
