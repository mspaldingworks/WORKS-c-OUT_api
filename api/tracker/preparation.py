"""
Bulk "prepare to apply": turn selected postings into queued applications with
tailored materials already written.

Generation runs ~40s per posting and is serial, so a batch of ten would sit far
past any sensible HTTP timeout. Preparation therefore runs on a background
thread and reports progress through a cache-backed record the app polls.

A thread (not Celery) because this is a single-user tool and the alternative is
standing up a worker for one button. If it proves unreliable — a container
restart mid-run loses the batch — Redis is already here and Celery is the
upgrade path. The user-visible cost of that failure is low: nothing is lost
except the in-flight generations, and re-running skips whatever finished.
"""

import logging
import threading
import uuid

from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)

# Long enough to outlive a slow batch, short enough that stale jobs age out.
JOB_TTL_SECONDS = 60 * 60


def _job_key(job_id):
    return f"prepare-job:{job_id}"


def get_job(job_id):
    return cache.get(_job_key(job_id))


def _save(job):
    cache.set(_job_key(job["id"]), job, JOB_TTL_SECONDS)


def prepare_postings(posting_ids):
    """Start a background prepare run. Returns the job record immediately."""
    job = {
        "id": uuid.uuid4().hex,
        "state": "running",
        "total": len(posting_ids),
        "done": 0,
        "results": [],
    }
    _save(job)

    thread = threading.Thread(target=_run, args=(job, list(posting_ids)), daemon=True)
    thread.start()
    return job


def _run(job, posting_ids):
    from ingestion.documents import build_documents
    from ingestion.generation import GenerationUnavailable, generate_materials
    from ingestion.models import IngestedPosting
    from ingestion.services import promote_posting_to_application
    from identity.models import ProfessionalProfile

    from .models import Application
    from .sheets import sync_sheet_quietly

    profile = ProfessionalProfile.objects.first()
    master_resume = profile.master_resume if profile else ""

    for posting_id in posting_ids:
        result = {"posting_id": posting_id, "ok": False, "detail": ""}
        try:
            posting = IngestedPosting.objects.get(pk=posting_id)
        except IngestedPosting.DoesNotExist:
            result["detail"] = "That posting no longer exists."
            job["results"].append(result)
            job["done"] += 1
            _save(job)
            continue

        # Generation is deliberately OUTSIDE the transaction: it's a slow network
        # call, and holding a database transaction open across it would pin a
        # connection for ~40s per posting.
        materials_error = ""
        if not posting.generated_materials:
            try:
                posting.generated_materials = generate_materials(posting, master_resume)
                posting.save(update_fields=["generated_materials"])
            except GenerationUnavailable as error:
                # Queue it anyway — a posting without a letter is still worth
                # tracking, and she can generate one by hand from the feed.
                materials_error = str(error)

        try:
            with transaction.atomic():
                application = (
                    Application.objects.filter(source_posting=posting).first()
                    or promote_posting_to_application(posting)
                )
                application.status = Application.Status.READY
                application.save(update_fields=["status"])
            # PDFs are what a portal actually accepts; render them here so the
            # queue is submit-ready rather than needing a second pass.
            build_documents(application)
            result["ok"] = True
            result["application_id"] = application.pk
            result["detail"] = materials_error or "Ready to submit."
        except Exception as error:
            logger.exception("Failed to prepare posting %s", posting_id)
            result["detail"] = f"Couldn't queue this one: {error}"

        job["results"].append(result)
        job["done"] += 1
        _save(job)

    job["state"] = "finished"
    _save(job)
    sync_sheet_quietly()
