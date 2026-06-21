"""Plain-python sanity tests for the hours conversion. Run:

    python3 tests/test_aggregate_core.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "aggregator"))
from aggregate_core import aggregate_rows, build_summary, load_for, make_conversion  # noqa: E402

REASON_MAP = {"TN": "Train operations", "IB": "Points failures"}
OPERATOR_MAP = {
    "HF": {"name": "Avanti West Coast", "class": "passenger"},
    "ED": {"name": "Northern Trains", "class": "passenger"},
    "WA": {"name": "DB Cargo", "class": "freight"},
}
PARAMS = {
    "default_passenger_load": 100,
    "freight_equiv_passengers": 10,
    "operator_load": {"HF": 200},   # ED has no override -> default 100
    "operator_load_year": "test",
}
CONV = make_conversion(PARAMS, OPERATOR_MAP)


def approx(a, b, tol=0.05):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_load_for():
    assert load_for("HF", CONV) == 200          # per-operator override
    assert load_for("ED", CONV) == 100          # passenger default
    assert load_for("WA", CONV) == 10           # freight equivalent
    assert load_for("??", CONV) == 100          # unknown -> passenger default


def test_aggregate_and_hours():
    rows = [
        # 60 min each so minutes/60 == 1, hours == the multiplier
        {"FINANCIAL_YEAR_PERIOD": "2024/25_P01", "TOC_CODE": "HF",
         "INCIDENT_REASON": "TN", "PFPI_MINUTES": "60", "NON_PFPI_MINUTES": "0"},
        {"FINANCIAL_YEAR_PERIOD": "2024/25_P01", "TOC_CODE": "ED",
         "INCIDENT_REASON": "TN", "PFPI_MINUTES": "60", "NON_PFPI_MINUTES": "0"},
        {"FINANCIAL_YEAR_PERIOD": "2024/25_P01", "TOC_CODE": "WA",
         "INCIDENT_REASON": "IB", "PFPI_MINUTES": "0", "NON_PFPI_MINUTES": "60"},
    ]
    per = aggregate_rows(rows, REASON_MAP, CONV["operator_class"])
    assert per["operators"]["HF"]["delay"] == 60
    assert per["categories"]["Points failures"]["frt_cancel"] == 60

    s = build_summary([per], period_end_dates={"2024/25_P01": "2024-04-27"},
                      window=13, conversion=CONV)

    # delay: HF 1h*200 + ED 1h*100 = 300 ; cancel: WA(freight) 1h*10 = 10
    approx(s["delay_hours"], 300)
    approx(s["cancellation_hours"], 10)
    approx(s["total_hours"], 310)
    approx(s["passenger_hours"], 300)    # HF + ED
    approx(s["freight_hours"], 10)       # WA
    # invariants the page relies on
    approx(s["passenger_hours"] + s["freight_hours"], s["total_hours"])
    approx(s["delay_hours"] + s["cancellation_hours"], s["total_hours"])
    approx(s["series"][0]["total_hours"], 310)
    assert s["schema_version"] == 3


if __name__ == "__main__":
    test_load_for()
    test_aggregate_and_hours()
    print("ok — all conversion tests passed")
