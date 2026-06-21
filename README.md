# UK train hours lost

A single-page site showing the total human-hours lost to train delays and
cancellations across Britain, from Network Rail's Historic Delay Attribution
dataset (RailData). The headline defaults to the rolling 12 months, with an
**all-time toggle** that re-scopes the whole page. An expandable breakdown
shows the delay/cancellation split, the top-10 incident reasons (each with a
plain-English description), and a month-by-month trend chart with a trend line.

**Live:** https://uk-train-hours-lost.web.app

## How it works

```
GCS bucket  nwr-historic-delay-attribution-data   (project: ukraildelaytracker)
  raw_zip-file-dump/<Transparency …>.zip   ← new file dropped ~monthly (automatic)
        │  object-finalize event
        ▼
  Cloud Function  nwr-delay-aggregator  (gen2, europe-west1, trigger region eu)
        │  • extracts the CSV from the zip, streams + aggregates it
        │  • writes derived/periods/<FY_PERIOD>.json   (private, idempotent per period)
        │  • rebuilds summary.json from the latest 13 periods
        ▼
  Public bucket  ukraildelaytracker-web/summary.json   (public-read + CORS)
        ▲  fetched at page load
  Firebase Hosting  index.html   (project: uk-train-hours-lost)
```

Two GCP projects are involved by design: the data, bucket and function live in
`ukraildelaytracker`; only the static page is hosted in `uk-train-hours-lost`.
The page reads data cross-project via the public `summary.json` URL.

## The calculation

Per row of the CSV: `PFPI_MINUTES` (delay) and `NON_PFPI_MINUTES` (cancellation)
are summed. Totals are stored as **minutes** (not hours), so the multiplier and
window stay adjustable without reprocessing.

- **Total human-hours** = (delay + cancellation minutes) × `people_per_train` / 60
- **Delay hours** = delay minutes × people_per_train / 60
- **Cancellation hours** = cancellation minutes × people_per_train / 60
- **Top 10 incident reasons**: `INCIDENT_REASON` codes are mapped to the glossary's
  *Incident Category Description* and aggregated. (Verified 100% of codes map.)

Note: `*_MINUTES` are not integers — the real data carries fractional minutes
(e.g. `1.5`, `6.35`), summed as floats and rounded only for display.

## Adjusting the "people per train" multiplier (currently 100)

The multiplier lives in **two** places:

1. Past data already summarised — the function writes the value it used into
   `summary.json` as `people_per_train`, and the page applies it at read time.
   To change it, update the function's env var and rebuild:
   ```bash
   gcloud functions deploy nwr-delay-aggregator --region=europe-west1 --gen2 \
     --update-env-vars=PEOPLE_PER_TRAIN=175 --source=aggregator   # then re-run backfill
   SUMMARY_BUCKET=ukraildelaytracker-web python scripts/backfill_gcs.py nwr-historic-delay-attribution-data
   ```
   (Other env vars: `WINDOW_PERIODS` default 13, `RAW_PREFIX`, `PERIODS_PREFIX`,
   `SUMMARY_BUCKET`, `SUMMARY_OBJECT`.)

## Layout

- `aggregator/` — Cloud Function source (`main.py`), pure logic (`aggregate_core.py`),
  bundled lookups (`incident_reason_map.json`, `period_end_dates.json`), `requirements.txt`.
- `scripts/build_lookups.py` — regenerate the lookup JSONs from the glossary xlsx (run when it changes).
- `scripts/run_local.py` — run the whole pipeline locally against sample files into `web/derived/`.
- `scripts/backfill_gcs.py` — process every zip already in the bucket and rebuild `summary.json`.
- `web/index.html` — the page.
- `firebase.json`, `.firebaserc` — Firebase Hosting config (`firebase deploy --only hosting`).

## Redeploy

```bash
# function
gcloud functions deploy nwr-delay-aggregator --project=ukraildelaytracker \
  --gen2 --runtime=python312 --region=europe-west1 --source=aggregator \
  --entry-point=on_zip_uploaded --trigger-location=eu \
  --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
  --trigger-event-filters="bucket=nwr-historic-delay-attribution-data" \
  --set-env-vars=SUMMARY_BUCKET=ukraildelaytracker-web --memory=1Gi --timeout=540s

# page
firebase deploy --only hosting --project uk-train-hours-lost
```
