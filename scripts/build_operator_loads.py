"""Build aggregator/conversion_params.json from ORR passenger-usage tables.

Tier 1 of the hours methodology: instead of one flat passengers-per-train, each
passenger operator gets its own average load, derived empirically as

    avg passengers per train = passenger-km / passenger-train-km

from the ORR Data Portal:
  - Table 1233 (passenger-km by operator):    https://dataportal.orr.gov.uk/.../table-1233-...
  - Table 1243 (passenger-train-km by operator): https://dataportal.orr.gov.uk/.../table-1243-...

Download both .ods files, then run:

    python scripts/build_operator_loads.py ~/Downloads/table-1233-*.ods ~/Downloads/table-1243-*.ods

It writes aggregator/conversion_params.json, preserving any hand-tuned
default_passenger_load / freight_equiv_passengers already present there.
"""
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

_ANNUAL_RE = re.compile(r"^Apr \d{4} to Mar \d{4}")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "aggregator" / "conversion_params.json"

# ORR operator name (as printed in the table header) -> TOC_CODE used in the
# Historic Delay Attribution feed. Curated rather than fuzzy-matched so the join
# is auditable. Operators absent here fall back to default_passenger_load.
ORR_NAME_TO_TOC = {
    "Avanti West Coast": "HF",
    "c2c": "HT",
    "Caledonian Sleeper": "ES",
    "Chiltern Railways": "HO",
    "CrossCountry": "EH",
    "East Midlands Railway": "EM",
    "Elizabeth line": "EX",
    "Govia Thameslink Railway": "ET",
    "Great Western Railway": "EF",
    "Greater Anglia": "EB",
    "London North Eastern Railway": "HB",
    "London Overground": "EK",
    "Merseyrail": "HE",
    "Northern Trains": "ED",
    "ScotRail": "HA",
    "South Western Railway": "HY",
    "Southeastern": "HU",
    "TfW Rail": "HL",
    "TransPennine Express": "EA",
    "West Midlands Trains": "EJ",
    "Grand Central": "EC",
    "Heathrow Express": "HM",
    "Hull Trains": "PF",
}

# Fallbacks used only if conversion_params.json doesn't already define them.
DEFAULT_PASSENGER_LOAD = 126   # ORR 2024-25 national avg (64.6bn pkm / 511.6m train-km)
DEFAULT_FREIGHT_EQUIV = 15     # TAG value-of-time bridge; see note below

_NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def _read_data_table(path):
    """Return rows of the worksheet that holds the data (has a 'Time period'
    header), skipping the Cover_sheet / Notes tabs."""
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("content.xml"))
    for tbl in root.iter("{%s}table" % _NS["table"]):
        rows = []
        for tr in tbl.findall("table:table-row", _NS):
            cells = []
            for tc in tr.findall("table:table-cell", _NS):
                rep = int(tc.get("{%s}number-columns-repeated" % _NS["table"], "1"))
                if rep > 50:
                    rep = 1  # trailing filler columns
                val = tc.get("{%s}value" % _NS["table"])
                if val is None:
                    val = "".join(t.text or "" for t in tc.iter("{%s}p" % _NS["text"]))
                cells.extend([val] * rep)
            rows.append(cells)
        if any(r and r[0] == "Time period" for r in rows):
            return rows
    return []


def _latest_annual(path):
    """{operator name -> float value} for the most recent 'Apr YYYY to Mar YYYY' row."""
    rows = _read_data_table(path)
    # A worksheet can stack several sub-tables (1243 has all/electric/diesel traction).
    # Use only the FIRST one (all-traction annual): from its 'Time period' header up to
    # the next header row.
    starts = [i for i, r in enumerate(rows) if r and r[0] == "Time period"]
    if not starts:
        raise SystemExit(f"Could not locate annual table in {path}")
    first, nxt = starts[0], (starts[1] if len(starts) > 1 else len(rows))
    block = rows[first:nxt]
    hdr = block[0]
    annual = [r for r in block if r and isinstance(r[0], str) and _ANNUAL_RE.match(r[0])]
    if not hdr or not annual:
        raise SystemExit(f"Could not locate annual table in {path}")
    data = annual[-1]
    out = {}
    for name, val in zip(hdr[1:], data[1:]):
        if not name:
            continue
        nm = name.split("(")[0].split("[note")[0].strip()
        try:
            out[nm] = float(val)
        except (TypeError, ValueError):
            pass
    return data[0], out


def main():
    if len(sys.argv) >= 3:
        p1233, p1243 = Path(sys.argv[1]), Path(sys.argv[2])
    else:
        dl = Path.home() / "Downloads"
        p1233 = next(dl.glob("table-1233-*.ods"), None)
        p1243 = next(dl.glob("table-1243-*.ods"), None)
    if not (p1233 and p1243 and p1233.exists() and p1243.exists()):
        raise SystemExit("Need ORR table 1233 (pax-km) and 1243 (train-km) .ods files.")

    year, pkm = _latest_annual(p1233)   # billions
    _, tkm = _latest_annual(p1243)       # millions

    loads = {}
    for name, code in ORR_NAME_TO_TOC.items():
        p, t = pkm.get(name), tkm.get(name)
        if p and t:
            loads[code] = round((p * 1e9) / (t * 1e6))

    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    params = {
        "version": 1,
        "generated_at": date.today().isoformat(),
        "default_passenger_load": existing.get("default_passenger_load", DEFAULT_PASSENGER_LOAD),
        "freight_equiv_passengers": existing.get("freight_equiv_passengers", DEFAULT_FREIGHT_EQUIV),
        "default_passenger_load_note": (
            "ORR national average passengers/train: total passenger-km / passenger-train-km."
        ),
        "freight_equiv_passengers_note": (
            "Equivalent passengers per freight/engineering train. Bridges a freight-train "
            "delay-hour to passenger-hours via DfT TAG value-of-time ratio "
            "(~freight VoT per train-hour / average passenger VoT per hour). TUNABLE."
        ),
        "operator_load_year": year,
        "operator_load_source": "ORR Data Portal tables 1233 & 1243 (passenger-km / passenger-train-km)",
        "operator_load": dict(sorted(loads.items())),
    }
    OUT.write_text(json.dumps(params, indent=2))
    print(f"Wrote {OUT.relative_to(ROOT)} for {year}")
    print(f"  default_passenger_load   = {params['default_passenger_load']}")
    print(f"  freight_equiv_passengers = {params['freight_equiv_passengers']}")
    print(f"  per-operator loads       = {len(loads)} operators")
    print("  range:", min(loads.values()), "->", max(loads.values()))


if __name__ == "__main__":
    main()
