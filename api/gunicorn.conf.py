"""
gunicorn settings that can't be given as flags. entrypoint.sh loads this with
--config; the flags there (workers, timeout, bind) still apply.
"""

import gc


def post_request(worker, req, environ, resp):
    # A DRF serializer and its .data refer to each other, so the rows behind a
    # finished response are not freed when the request ends — they wait for a
    # full collection, and several requests' worth pile up first. With the
    # posting feed at ~3 MB a response, that held each worker near 180 MB and
    # the container against its 384 MB limit. Collecting here keeps a worker
    # near 120 MB. It costs a few tens of milliseconds, after the response has
    # already been sent.
    gc.collect()
