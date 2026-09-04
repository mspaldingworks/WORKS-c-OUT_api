#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Cloud Run starts many instances; only one should migrate. Harmless here
# because migrate is idempotent, but noted so it isn't mistaken for a design.

# --timeout 300: generating tailored application materials is a single long
# model call that runs well past gunicorn's 30s default, and the worker gets
# killed mid-request otherwise (shows up as a bare 500).
# Cloud Run injects $PORT and routes to it; locally there's no PORT and 8000 is
# what docker-compose publishes. One entrypoint serves both.
exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 300
