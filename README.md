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
are summed, **broken down by operator** (`TOC_CODE`). Per-period totals are stored
as raw **minutes** so the conversion stays adjustable without reprocessing; the
summary build then converts minutes to a single "equivalent person-hours" figure:

- **Passenger trains** → `delay_minutes / 60 × passengers-per-train[operator]`.
  Loads are operator-specific, derived from ORR passenger-km ÷ train-km
  (`conversion_params.json`), defaulting to the national average (~126) for
  operators not in the table.
- **Freight / engineering trains** (no passengers) → `delay_minutes / 60 ×
  freight_equiv_passengers`, a single constant that bridges a freight-train
  delay-hour to *equivalent* passenger-hours via the DfT TAG value-of-time ratio.
- **Total hours** = passenger hours + freight hours (one commensurable number).
- **Top 10 incident reasons**: `INCIDENT_REASON` codes are mapped to the glossary's
  *Incident Category Description*, split passenger/freight, and converted to hours.

Operator passenger vs freight is classified in `operator_map.json` (from the
glossary's *Operator Name* sheet). Note: `*_MINUTES` are not integers — the real
data carries fractional minutes (e.g. `1.5`, `6.35`), summed as floats.

## Adjusting the conversion (per-operator loads, national default, freight)

- **Per-operator passenger loads** come from ORR tables 1233 & 1243. Download both
  `.ods` files and regenerate `aggregator/conversion_params.json`:
  ```bash
  python scripts/build_operator_loads.py ~/Downloads/table-1233-*.ods ~/Downloads/table-1243-*.ods
  ```
- **National default load** and **freight equivalent** can be tweaked in
  `conversion_params.json`, or overridden per-deploy without re-running the script:
  ```bash
  gcloud functions deploy nwr-delay-aggregator --region=europe-west1 --gen2 \
    --update-env-vars=DEFAULT_PASSENGER_LOAD=130,FREIGHT_EQUIV_PASSENGERS=20 --source=aggregator
  SUMMARY_BUCKET=ukraildelaytracker-web python scripts/backfill_gcs.py nwr-historic-delay-attribution-data
  ```
  The page reads the headline numbers from `summary.json`, so changing them
  needs a backfill (the conversion is no longer applied client-side).
  (Other env vars: `WINDOW_PERIODS` default 13, `RAW_PREFIX`, `PERIODS_PREFIX`,
  `SUMMARY_BUCKET`, `SUMMARY_OBJECT`.)

## Layout

- `aggregator/` — Cloud Function source (`main.py`), pure logic (`aggregate_core.py`),
  bundled lookups (`incident_reason_map.json`, `period_end_dates.json`,
  `operator_map.json`, `conversion_params.json`), `requirements.txt`.
- `scripts/build_lookups.py` — regenerate the glossary-derived lookups (incident reasons,
  period end dates, operator map) from the glossary xlsx (run when it changes).
- `scripts/build_operator_loads.py` — regenerate per-operator passenger loads in
  `conversion_params.json` from the ORR `.ods` tables (1233 & 1243).
- `tests/test_aggregate_core.py` — sanity tests for the hours conversion.
- `scripts/run_local.py` — run the whole pipeline locally against sample files into `web/derived/`.
- `scripts/backfill_gcs.py` — process every zip already in the bucket and rebuild `summary.json`.
- `web/index.html` — the page.
- `firebase.json`, `.firebaserc` — Firebase Hosting config (`firebase deploy --only hosting`).
- `.gitignore` — excludes machine-local/generated files (`web/derived/` sample output, `.firebase/` cache, `__pycache__/`, `.DS_Store`, and any `*-key.json`/`.env` secrets).

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
