"""Run the aggregation pipeline locally against sample files.

Processes each given .zip or .csv (default: the Transparency samples in
~/Downloads), writes one per-period summary to web/derived/periods/, then
rebuilds web/derived/summary.json — exactly what the cloud function produces,
so the webpage can be developed and verified offline.

    python scripts/run_local.py [file1.zip file2.csv ...]
"""
import csv
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "aggregator"))
from aggregate_core import (  # noqa: E402
    aggregate_rows, build_summary, make_conversion, operator_class_map,
)

ROOT = Path(__file__).resolve().parent.parent
AGG = ROOT / "aggregator"
PERIODS_DIR = ROOT / "web" / "derived" / "periods"
SUMMARY_PATH = ROOT / "web" / "derived" / "summary.json"

DEFAULT_INPUTS = [
    Path.home() / "Downloads" / "Transparency 24-25 P11.csv",
    Path.home() / "Downloads" / "Transparency 26-27 P01.csv",
    Path.home() / "Downloads" / "raw_zip-file-dump_Transparency 24-25 P11 20251127.zip",
    Path.home() / "Downloads" / "Transparency 26-27 P01 20260604.zip",
]


def safe_name(period):
    return period.replace("/", "-").replace(" ", "_")


def rows_from_file(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError(f"No CSV inside {path.name}")
            with zf.open(csv_names[0]) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                yield from csv.DictReader(text)
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)


def main():
    reason_map = json.loads((AGG / "incident_reason_map.json").read_text())
    period_dates = json.loads((AGG / "period_end_dates.json").read_text())
    operator_map = json.loads((AGG / "operator_map.json").read_text())
    conversion_params = json.loads((AGG / "conversion_params.json").read_text())
    operator_class = operator_class_map(operator_map)
    conversion = make_conversion(conversion_params, operator_map)
    PERIODS_DIR.mkdir(parents=True, exist_ok=True)

    inputs = [Path(p) for p in sys.argv[1:]] or [p for p in DEFAULT_INPUTS if p.exists()]
    if not inputs:
        print("No input files found.")
        return

    for path in inputs:
        if not path.exists():
            print(f"  skip (missing): {path}")
            continue
        summary = aggregate_rows(rows_from_file(path), reason_map, operator_class)
        period = summary["period"]
        if not period:
            print(f"  skip (no period detected): {path.name}")
            continue
        out = PERIODS_DIR / f"{safe_name(period)}.json"
        out.write_text(json.dumps(summary, indent=2))
        um = summary["unmatched_codes"]
        print(f"  {path.name} -> period {period} | rows {summary['rows']:,} | "
              f"delay {summary['delay_minutes']:,} | cancel {summary['cancellation_minutes']:,}"
              + (f" | UNMATCHED {um}" if um else ""))

    # Rebuild summary.json from every per-period file on disk.
    all_periods = [json.loads(p.read_text()) for p in PERIODS_DIR.glob("*.json")]
    summary = build_summary(
        all_periods,
        period_end_dates=period_dates,
        conversion=conversion,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))

    print(f"\nsummary.json -> {summary['window_periods']} period(s) "
          f"{summary['period_first']}..{summary['period_last']} "
          f"({summary['coverage_start']}..{summary['coverage_end']})")
    print(f"  total hours: {round(summary['total_hours']):,} "
          f"(passenger {round(summary['passenger_hours']):,} / "
          f"freight {round(summary['freight_hours']):,})")


if __name__ == "__main__":
    main()
