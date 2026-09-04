import datetime

from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Application, ApplicationEvent, Company, Contact
from .preparation import get_job, prepare_postings
from .serializers import ApplicationEventSerializer, ApplicationSerializer, CompanySerializer, ContactSerializer
from .sheets import SheetUnavailable, sync_sheet, sync_sheet_quietly


class CompanyViewSet(viewsets.ModelViewSet):
    queryset = Company.objects.all()
    serializer_class = CompanySerializer


class ContactViewSet(viewsets.ModelViewSet):
    queryset = Contact.objects.all()
    serializer_class = ContactSerializer


class ApplicationViewSet(viewsets.ModelViewSet):
    queryset = (
        Application.objects.select_related("company", "source_posting")
        .prefetch_related("events")
        .all()
    )
    serializer_class = ApplicationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    @action(detail=False, methods=["post"])
    def prepare(self, request):
        """
        Queue several postings for application in one go: generate materials,
        create the Application rows, and mark them ready to submit.

        Returns a job id immediately rather than blocking — generation is ~40s
        per posting and a batch would outlive any reasonable request timeout.
        """
        posting_ids = request.data.get("posting_ids") or []
        if not isinstance(posting_ids, list) or not posting_ids:
            return Response(
                {"detail": "Send posting_ids as a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(prepare_postings(posting_ids), status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["get"], url_path=r"prepare/(?P<job_id>[0-9a-f]+)")
    def prepare_status(self, request, job_id=None):
        job = get_job(job_id)
        if job is None:
            return Response({"detail": "No such prepare job."}, status=status.HTTP_404_NOT_FOUND)
        return Response(job)

    @action(detail=True, methods=["post"])
    def documents(self, request, pk=None):
        """(Re)render the cover letter and resume PDFs for this application."""
        from ingestion.documents import build_documents

        application = self.get_object()
        written = build_documents(application)
        if not written:
            return Response(
                {"detail": "Nothing to render — this posting has no generated materials yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"written": written})

    @action(detail=True, methods=["get"], url_path=r"download/(?P<kind>resume|cover-letter)")
    def download(self, request, pk=None, kind=None):
        """
        Stream a PDF through token auth.

        Deliberately not a public /media/ URL: these carry her full contact
        details and a letter written for one employer, and file names are
        guessable enough that "unlisted" would not be private.
        """
        application = self.get_object()
        field = application.resume if kind == "resume" else application.cover_letter
        if not field:
            raise Http404("That document hasn't been rendered yet.")
        return FileResponse(field.open("rb"), content_type="application/pdf",
                            as_attachment=True, filename=field.name.rsplit("/", 1)[-1])

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """
        Record that she's read the draft and okayed it.

        Deliberately separate from submitting: nothing is sent anywhere by this,
        and nothing downstream treats an unapproved draft as ready to go out.
        """
        application = self.get_object()
        application.status = Application.Status.APPROVED
        application.save(update_fields=["status"])
        sync_sheet_quietly()
        return Response(self.get_serializer(application).data)

    @action(detail=True, methods=["patch"], url_path="materials")
    def edit_materials(self, request, pk=None):
        """
        Replace the generated text with her edits, then re-render and re-upload.

        Her wording beats the model's, so this overwrites the cached materials
        on the posting — a later "Redo" would regenerate from scratch, which is
        the intended escape hatch rather than an accident.
        """
        from ingestion.documents import build_documents

        application = self.get_object()
        posting = application.source_posting
        if not posting:
            return Response(
                {"detail": "This application isn't linked to a posting, so there's nothing to edit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        materials = dict(posting.generated_materials or {})
        for field in ("cover_letter", "resume_summary"):
            if field in request.data:
                materials[field] = str(request.data[field])
        for field in ("resume_bullets", "gaps"):
            if field in request.data:
                value = request.data[field]
                if not isinstance(value, list):
                    return Response({"detail": f"{field} must be a list."},
                                    status=status.HTTP_400_BAD_REQUEST)
                materials[field] = [str(item) for item in value]

        posting.generated_materials = materials
        posting.save(update_fields=["generated_materials"])
        build_documents(application)
        application.refresh_from_db()
        sync_sheet_quietly()
        return Response(self.get_serializer(application).data)

    @action(detail=True, methods=["post"])
    def discard(self, request, pk=None):
        """
        Remove an application from the pipeline before it was ever sent.

        Not a delete: the generated text cost real money, the PDFs exist in
        Drive, and undo has to be able to put it back. The source posting is
        dismissed too, so it doesn't reappear in the feed tomorrow.
        """
        from ingestion.models import IngestedPosting

        application = self.get_object()
        application.status = Application.Status.DISCARDED
        application.save(update_fields=["status"])

        if posting := application.source_posting:
            posting.status = IngestedPosting.Status.DISMISSED
            posting.save(update_fields=["status"])

        sync_sheet_quietly()
        return Response(self.get_serializer(application).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """Undo a discard, putting it back where it was."""
        application = self.get_object()
        application.status = Application.Status.READY
        application.save(update_fields=["status"])
        sync_sheet_quietly()
        return Response(self.get_serializer(application).data)

    @action(detail=True, methods=["post"], url_path="mark-applied")
    def mark_applied(self, request, pk=None):
        """Flip a queued application to applied and stamp the date."""
        application = self.get_object()
        application.status = Application.Status.APPLIED
        if not application.applied_date:
            application.applied_date = datetime.date.today()
        application.save(update_fields=["status", "applied_date"])
        sync_sheet_quietly()
        return Response(self.get_serializer(application).data)

    @action(detail=False, methods=["post"], url_path="sync-sheet")
    def sync_sheet_now(self, request):
        """Manual full rewrite of the Google Sheet, for the app's refresh button."""
        try:
            count = sync_sheet()
        except SheetUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"synced": count})


class ApplicationEventViewSet(viewsets.ModelViewSet):
    queryset = ApplicationEvent.objects.all()
    serializer_class = ApplicationEventSerializer
