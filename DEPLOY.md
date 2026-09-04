# WORKS(c)OUT API

Django + DRF. Split out of Family Appily; shares no repo, container, database,
network, port or signing identity with it.

## Guardrails

| | Family Appily | WORKS(c)OUT |
|---|---|---|
| Compose project | `family-appily-api` | `works-c-out-api` |
| Docker network | `family_appily_internal` | `works_c_out_internal` |
| Host volumes | `/srv/docker/family-appily-api/` | `/srv/docker/works-c-out-api/` |
| Loopback port | 8004 | 8005 |
| Database | `family_appily` | `works_c_out` |
| iOS bundle | `com.mspaldingworks.FamilyAppily` | `com.mspaldingworks.WorksCout` |
| Mac bundle | `com.mspaldingworks.FamilyAppily` | `com.mspaldingworks.WorksCoutMac` |
| Keychain service | `…FamilyAppily.jobsearch` | `…WorksCout.token` |
| Xcode project | `FamilyAppily.xcodeproj` | `WorksCout.xcodeproj` |

Nothing is shared, so either can be rebuilt, redeployed or deleted without
touching the other. **Never** run `docker compose down -v` or `docker volume
prune` on the shared VPS — Postgres data and media live in volumes there.

## Cloud Run

The image is the same one compose builds; the entrypoint binds `$PORT` when
Cloud Run sets it and 8000 otherwise.

```bash
gcloud run deploy works-c-out-api \
  --source api \
  --region us-central1 \
  --add-cloudsql-instances "$CLOUD_SQL_CONNECTION_NAME" \
  --set-env-vars "CLOUD_SQL_CONNECTION_NAME=$CLOUD_SQL_CONNECTION_NAME" \
  --set-secrets "DJANGO_SECRET_KEY=django-secret:latest,POSTGRES_PASSWORD=db-password:latest,ANTHROPIC_API_KEY=anthropic-key:latest"
```

### Two things Cloud Run changes

**Media is ephemeral.** Generated PDFs are written to the container filesystem,
which Cloud Run discards on every restart. The durable copies are in Drive, and
the app links to those, but `/download/resume/` will 404 after a restart until
the documents are re-rendered. Fix properly with a GCS bucket via
`django-storages` before this matters.

**Prepare-job progress lives in the cache.** Without `REDIS_URL` it's per
instance, so a poll that lands on a different instance sees no job. Either keep
`--max-instances 1` or attach Memorystore.

## Local

```bash
docker compose --project-name works-c-out-api \
  -f docker-compose/prod-docker-compose.yml up -d --build
docker exec works-c-out-api-web-1 python manage.py test
```
