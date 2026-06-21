"""Cloud Run function: aggregate a newly-dropped Transparency zip and rebuild
the rolling-12-month summary the webpage reads.

Triggered by GCS object-finalize (Eventarc) on the raw zip bucket/prefix.
The same helpers are reused by scripts/backfill_gcs.py for the initial load.

Environment variables (all optional, with sensible defaults):
  RAW_PREFIX        prefix to watch for zips     (default "raw_zip-file-dump/")
  PERIODS_PREFIX    where per-period JSON is kept (default "derived/periods/")
  SUMMARY_BUCKET    bucket for the public summary (default = source bucket)
  SUMMARY_OBJECT    object name for the summary   (default "summary.json")
  PEOPLE_PER_TRAIN  passengers-per-train estimate (default in code; may be revised)
  WINDOW_PERIODS    rolling window in periods     (default 13)
"""
import csv
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import functions_framework
from google.cloud import storage

from aggregate_core import aggregate_rows, build_summary

_HERE = Path(__file__).resolve().parent
REASON_MAP = json.loads((_HERE / "incident_reason_map.json").read_text())
PERIOD_END_DATES = json.loads((_HERE / "period_end_dates.json").read_text())

RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw_zip-file-dump/")
PERIODS_PREFIX = os.environ.get("PERIODS_PREFIX", "derived/periods/")
SUMMARY_OBJECT = os.environ.get("SUMMARY_OBJECT", "summary.json")
PEOPLE_PER_TRAIN = int(os.environ.get("PEOPLE_PER_TRAIN", "100"))
WINDOW_PERIODS = int(os.environ.get("WINDOW_PERIODS", "13"))

_client = None


def _gcs():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _safe_name(period):
    return period.replace("/", "-").replace(" ", "_")


def process_zip(bucket_name, object_name):
    """Download one zip, aggregate it, write its per-period summary. Returns
    the per-period dict (or None if it wasn't a processable zip)."""
    if not object_name.lower().endswith(".zip"):
        return None

    bucket = _gcs().bucket(bucket_name)
    data = bucket.blob(object_name).download_as_bytes()

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            print(f"No CSV inside {object_name}; skipping.")
            return None
        with zf.open(csv_names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            summary = aggregate_rows(csv.DictReader(text), REASON_MAP)

    period = summary["period"]
    if not period:
        print(f"No FINANCIAL_YEAR_PERIOD found in {object_name}; skipping.")
        return None

    out_name = f"{PERIODS_PREFIX}{_safe_name(period)}.json"
    bucket.blob(out_name).upload_from_string(
        json.dumps(summary), content_type="application/json"
    )
    if summary["unmatched_codes"]:
        print(f"WARNING unmatched incident codes in {period}: {summary['unmatched_codes']}")
    print(f"Wrote {out_name}: period {period}, rows {summary['rows']}, "
          f"delay {summary['delay_minutes']}, cancel {summary['cancellation_minutes']}")
    return summary


def rebuild_summary(source_bucket_name):
    """Recompile summary.json from every per-period JSON in the source bucket."""
    src = _gcs().bucket(source_bucket_name)
    period_summaries = []
    for blob in _gcs().list_blobs(source_bucket_name, prefix=PERIODS_PREFIX):
        if blob.name.endswith(".json"):
            period_summaries.append(json.loads(blob.download_as_bytes()))

    summary = build_summary(
        period_summaries,
        period_end_dates=PERIOD_END_DATES,
        window=WINDOW_PERIODS,
        people_per_train=PEOPLE_PER_TRAIN,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    summary_bucket = _gcs().bucket(os.environ.get("SUMMARY_BUCKET", source_bucket_name))
    blob = summary_bucket.blob(SUMMARY_OBJECT)
    blob.cache_control = "public, max-age=300"
    blob.upload_from_string(json.dumps(summary), content_type="application/json")
    print(f"Rebuilt {summary_bucket.name}/{SUMMARY_OBJECT}: "
          f"{summary['window_periods']} periods, total_minutes {summary['total_minutes']}")
    return summary


@functions_framework.cloud_event
def on_zip_uploaded(cloud_event):
    """Eventarc entrypoint for google.cloud.storage.object.v1.finalized."""
    data = cloud_event.data
    bucket_name = data["bucket"]
    object_name = data["name"]

    if not object_name.startswith(RAW_PREFIX) or not object_name.lower().endswith(".zip"):
        print(f"Ignoring {object_name} (not a zip under {RAW_PREFIX}).")
        return

    print(f"Processing gs://{bucket_name}/{object_name}")
    if process_zip(bucket_name, object_name) is not None:
        rebuild_summary(bucket_name)
