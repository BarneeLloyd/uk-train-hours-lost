"""One-off backfill: process every Transparency zip already sitting in the
bucket, then build summary.json. Run once before/after deploying the trigger.

    SUMMARY_BUCKET=<public-bucket> python scripts/backfill_gcs.py <source-bucket>

Reuses the exact code path the live Cloud Function uses.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "aggregator"))

import main  # noqa: E402  (configured via env before import side effects matter)
from google.cloud import storage  # noqa: E402


def run(source_bucket):
    client = storage.Client()
    zips = [
        b.name for b in client.list_blobs(source_bucket, prefix=main.RAW_PREFIX)
        if b.name.lower().endswith(".zip")
    ]
    print(f"Found {len(zips)} zip(s) under {source_bucket}/{main.RAW_PREFIX}")
    processed = 0
    for name in sorted(zips):
        if main.process_zip(source_bucket, name) is not None:
            processed += 1
    print(f"Processed {processed} zip(s); rebuilding summary...")
    main.rebuild_summary(source_bucket)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/backfill_gcs.py <source-bucket>")
        sys.exit(1)
    run(sys.argv[1])
