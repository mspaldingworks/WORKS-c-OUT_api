"""
Choosing files and a save folder from a person's own Google Drive.

The Drive connection asks only for drive.file: the app sees files it created
and nothing else. Google's Picker is how a person hands it one more — a file
picked there becomes visible to the app, and only that file. Asking for wider
read access instead would put the app through Google's restricted-scope
security review and show everyone a "see all your Drive files" consent screen.

The flow, one browser hop like connecting Drive:

1. The app asks `drive/picker-link/` for a link (mode = documents or folder).
2. It opens that link in an in-app browser session. The page loads the Picker
   with a short-lived access token for this person's Drive.
3. Picking (or cancelling) sends the browser to `workscout://drive-picked?…`,
   which closes the session and hands the app the chosen file ids.
4. The app posts those ids to `drive/import/` (documents) or `drive/folder/`.

The link is signed, expires in ten minutes and works once: it is what turns
into an access token on the page, so it must not be replayable.
"""

import json
import logging
import secrets
import urllib.parse
from pathlib import PurePath

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import escape
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import google_oauth
from .models import GoogleDriveConnection, ResumeVersion
from .serializers import ResumeVersionSerializer
from .validators import MAX_RESUME_BYTES

logger = logging.getLogger(__name__)

_SALT = "identity.drive.picker"
_MAX_AGE = 600
MODES = {"documents", "folder"}
MAX_PICKED = 10


def picker_configured():
    return bool(
        settings.GOOGLE_PICKER_API_KEY and settings.GOOGLE_CLOUD_PROJECT_NUMBER
        and google_oauth.client_configured()
    )


def _connection(user):
    connection = GoogleDriveConnection.objects.filter(owner=user).first()
    return connection if connection and connection.connected else None


def _credentials(connection):
    from tracker.drive import credentials_for

    return credentials_for(connection.get_token())


def _unavailable(detail, code=status.HTTP_503_SERVICE_UNAVAILABLE):
    return Response({"detail": detail}, status=code)


class DrivePickerLinkView(APIView):
    """GET ?mode=documents|folder → {"picker_url": …}, one use, ten minutes."""

    def get(self, request):
        mode = request.query_params.get("mode", "documents")
        if mode not in MODES:
            return _unavailable("Unknown picker mode.", status.HTTP_400_BAD_REQUEST)
        if not picker_configured():
            return _unavailable("Choosing files from Google Drive isn't set up on the server yet.")
        if _connection(request.user) is None:
            return _unavailable("Connect Google Drive first.", status.HTTP_409_CONFLICT)

        ticket = signing.dumps({"uid": request.user.pk, "mode": mode, "n": secrets.token_hex(16)}, salt=_SALT)
        url = request.build_absolute_uri(reverse("drive-picker")) + "?" + urllib.parse.urlencode({"t": ticket})
        return Response({"picker_url": url})


def _return_url(**params):
    return f"{settings.GOOGLE_PICKER_RETURN_URL}?{urllib.parse.urlencode(params)}"


def _page(body, status_code=200):
    response = HttpResponse(body, status=status_code, content_type="text/html; charset=utf-8")
    # The page carries an access token: never cache it, never leak the URL.
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _bounce(**params):
    """A page that sends the browser straight back to the app."""
    url = escape(_return_url(**params))
    return _page(
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={url}">'
        f'<p style="font-family:-apple-system,sans-serif">Returning to WORKS(c)OUT… '
        f'<a href="{url}">Continue</a></p>'
    )


class DrivePickerPageView(APIView):
    """The browser page that hosts Google's Picker. Authorised by its ticket
    alone — the in-app browser has no app token to send."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        try:
            data = signing.loads(request.query_params.get("t", ""), salt=_SALT, max_age=_MAX_AGE)
        except signing.BadSignature:
            return _bounce(error="This link expired. Try again from the app.")
        if not cache.add(f"drive-picker-used:{data['n']}", 1, _MAX_AGE):
            return _bounce(error="This link was already used. Try again from the app.")

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=data["uid"], is_active=True).first()
        connection = _connection(user) if user else None
        if connection is None or not picker_configured():
            return _bounce(error="Google Drive isn't connected.")

        try:
            from tracker.drive import access_token

            token = access_token(_credentials(connection))
        except Exception:
            logger.exception("Picker token refresh failed for %s", user.pk)
            return _bounce(error="Google Drive needs reconnecting. Disconnect and connect it again in Identity.")

        config = {
            "mode": data["mode"],
            "token": token,
            "key": settings.GOOGLE_PICKER_API_KEY,
            "appId": settings.GOOGLE_CLOUD_PROJECT_NUMBER,
            "returnUrl": settings.GOOGLE_PICKER_RETURN_URL,
        }
        # json.dumps then </ escaped, so nothing in a value can close the script.
        payload = json.dumps(config).replace("</", "<\\/")
        return _page(PICKER_PAGE.replace("__CONFIG__", payload))


PICKER_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Choose from Google Drive</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         color: #1d1d1f; background: #fff; }
  @media (prefers-color-scheme: dark) { body { color: #f5f5f7; background: #000; } }
  a { color: #0a84ff; }
</style>
</head><body>
<p id="status">Opening Google Drive…</p>
<p><a id="cancel" href="#">Cancel</a></p>
<script>
const config = __CONFIG__;
function back(params) {
  const query = new URLSearchParams(params).toString();
  window.location.href = config.returnUrl + "?" + query;
}
document.getElementById("cancel").addEventListener("click", function (event) {
  event.preventDefault();
  back({ mode: config.mode, cancelled: "1" });
});
function picked(data) {
  const action = data[google.picker.Response.ACTION];
  if (action === google.picker.Action.PICKED) {
    const docs = data[google.picker.Response.DOCUMENTS] || [];
    back({
      mode: config.mode,
      ids: docs.map(function (d) { return d[google.picker.Document.ID]; }).join(","),
      names: JSON.stringify(docs.map(function (d) { return d[google.picker.Document.NAME]; })),
    });
  } else if (action === google.picker.Action.CANCEL) {
    back({ mode: config.mode, cancelled: "1" });
  }
}
function openPicker() {
  let view;
  const builder = new google.picker.PickerBuilder();
  if (config.mode === "folder") {
    view = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
      .setIncludeFolders(true)
      .setSelectFolderEnabled(true)
      .setMimeTypes("application/vnd.google-apps.folder");
    builder.setTitle("Choose where WORKS(c)OUT saves your files");
  } else {
    view = new google.picker.DocsView(google.picker.ViewId.DOCS)
      .setIncludeFolders(true)
      .setMimeTypes([
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.google-apps.document",
        "text/plain", "text/markdown", "application/rtf", "text/rtf"
      ].join(","));
    builder.setTitle("Choose résumés or cover letters")
      .enableFeature(google.picker.Feature.MULTISELECT_ENABLED)
      .setMaxItems(10);
  }
  builder.addView(view)
    .setOAuthToken(config.token)
    .setDeveloperKey(config.key)
    .setAppId(config.appId)
    .setCallback(picked)
    .build()
    .setVisible(true);
  document.getElementById("status").textContent = "Choose in the Google Drive window.";
}
</script>
<script src="https://apis.google.com/js/api.js"
        onload="gapi.load('picker', { callback: openPicker })"
        onerror="document.getElementById('status').textContent = 'Couldn\\'t load Google Drive. Check your connection and try again.'"></script>
</body></html>
"""


class DriveImportView(APIView):
    """
    POST {"file_ids": [...], "kind": "resume"|"cover_letter"} — copy picked
    Drive documents into this account, through the same checks as an upload.
    Returns the documents created and, per file, any that were refused.
    """

    def post(self, request):
        connection = _connection(request.user)
        if connection is None:
            return _unavailable("Connect Google Drive first.", status.HTTP_409_CONFLICT)

        ids = request.data.get("file_ids") if isinstance(request.data, dict) else None
        kind = (request.data.get("kind") if isinstance(request.data, dict) else None) or ResumeVersion.Kind.RESUME
        if kind not in ResumeVersion.Kind.values:
            return _unavailable("Unknown document kind.", status.HTTP_400_BAD_REQUEST)
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            return _unavailable("Pick at least one file.", status.HTTP_400_BAD_REQUEST)

        from tracker.drive import DriveUnavailable, download_document, file_metadata

        from .document_storage import apply_storage

        credentials = _credentials(connection)
        created, refused = [], []
        for file_id in ids[:MAX_PICKED]:
            try:
                name, data = download_document(credentials, file_id, MAX_RESUME_BYTES)
            except DriveUnavailable as error:
                refused.append({"file_id": file_id, "detail": str(error)})
                continue
            except Exception:
                logger.exception("Drive import failed for %s", request.user.pk)
                refused.append({"file_id": file_id, "detail": "Couldn't download this file from Drive."})
                continue

            serializer = ResumeVersionSerializer(
                data={"title": PurePath(name).stem[:200] or "Document", "kind": kind,
                      "file": SimpleUploadedFile(name, data)},
                context={"request": request},
            )
            if serializer.is_valid():
                try:
                    drive_url = file_metadata(credentials, file_id).get("webViewLink", "")
                except Exception:
                    # Only the link to open it in Drive; the id still finds it.
                    drive_url = ""
                # The picked original is this document's Drive copy: never
                # uploaded again, and the only copy for Drive-only accounts.
                document = serializer.save(owner=request.user, drive_file_id=file_id, drive_url=drive_url)
                created.append(apply_storage(document))
            else:
                messages = [str(m) for errors in serializer.errors.values() for m in errors]
                refused.append({"file_id": file_id, "name": name, "detail": " ".join(messages)})

        body = {
            "created": ResumeVersionSerializer(created, many=True, context={"request": request}).data,
            "refused": refused,
        }
        # 200 when nothing was taken, not 400: the per-file reasons are the
        # answer, and a 400 body would be read as a validation error instead.
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class DriveFolderView(APIView):
    """
    POST {"folder_id": "…"} — save generated files into this Drive folder
    instead of the app's own. {"folder_id": ""} goes back to the app's folder.
    """

    def post(self, request):
        connection = _connection(request.user)
        if connection is None:
            return _unavailable("Connect Google Drive first.", status.HTTP_409_CONFLICT)

        from tracker.drive import DriveUnavailable, ensure_folder, folder_metadata

        folder_id = str(request.data.get("folder_id") or "").strip() if isinstance(request.data, dict) else ""
        credentials = _credentials(connection)
        try:
            if folder_id:
                meta = folder_metadata(credentials, folder_id)
                connection.folder_id, connection.folder_name = meta["id"], meta.get("name", "")
            else:
                connection.folder_id, connection.folder_name = ensure_folder(credentials), ""
        except DriveUnavailable as error:
            return _unavailable(str(error), status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Drive folder change failed for %s", request.user.pk)
            return _unavailable("Couldn't reach Google Drive. Try again.")
        connection.save(update_fields=["folder_id", "folder_name", "updated_at"])

        from .views import _drive_status

        return Response(_drive_status(connection))
