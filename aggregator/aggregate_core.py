"""Core aggregation logic — pure, no cloud dependencies, easy to unit-test.

Given the rows of one Transparency CSV (one railway period) and the
INCIDENT_REASON -> category map, produce a tiny summary:

    {
      "period": "2024/25_P11",
      "rows": 455580,
      "delay_minutes": 2542929,          # sum of PFPI_MINUTES
      "cancellation_minutes": 1211065,   # sum of NON_PFPI_MINUTES
      "categories": {
         "Train operations": {"delay": 1144112, "cancellation": 1480}, ...
      },
      "unmatched_codes": {"ZZ": 4}       # codes not in the glossary (should be empty)
    }

We store raw MINUTES (never the x150 / hours result) so the people-per-train
multiplier and the rolling window stay configurable at read time.
"""
import re
from collections import Counter, defaultdict

# Default rolling window: 13 four-week railway periods ~= 12 months.
DEFAULT_WINDOW = 13
DEFAULT_PEOPLE_PER_TRAIN = 100
DEFAULT_TOP_N = 10

_PERIOD_RE = re.compile(r"^(\d{4})/(\d{2})_P(\d{1,2})$")


def period_sort_key(period):
    """Order period codes like '2024/25_P11' chronologically -> (2024, 11)."""
    m = _PERIOD_RE.match(period or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(3)))


def _to_minutes(value):
    """Parse a minutes field. Real data carries fractional minutes
    (e.g. '1.5', '6.35'), so accumulate as float and round only for display."""
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def aggregate_rows(dict_rows, reason_map):
    """dict_rows: iterable of dict-like rows (e.g. from csv.DictReader)."""
    periods = Counter()
    rows = 0
    delay_total = 0.0
    cancel_total = 0.0
    cats = defaultdict(lambda: {"delay": 0.0, "cancellation": 0.0})
    unmatched = Counter()

    for row in dict_rows:
        rows += 1
        period = (row.get("FINANCIAL_YEAR_PERIOD") or "").strip()
        if period:
            periods[period] += 1

        pfpi = _to_minutes(row.get("PFPI_MINUTES"))
        non_pfpi = _to_minutes(row.get("NON_PFPI_MINUTES"))
        delay_total += pfpi
        cancel_total += non_pfpi

        code = (row.get("INCIDENT_REASON") or "").strip()
        category = reason_map.get(code)
        if category is None:
            if code:
                unmatched[code] += 1
            category = "Unattributed / unknown"
        elif category == "":
            category = "Unattributed / unknown"

        bucket = cats[category]
        bucket["delay"] += pfpi
        bucket["cancellation"] += non_pfpi

    period = periods.most_common(1)[0][0] if periods else None

    return {
        "period": period,
        "rows": rows,
        "delay_minutes": round(delay_total, 2),
        "cancellation_minutes": round(cancel_total, 2),
        "categories": {
            k: {"delay": round(v["delay"], 2), "cancellation": round(v["cancellation"], 2)}
            for k, v in cats.items()
        },
        "unmatched_codes": dict(unmatched),
    }


def build_summary(
    period_summaries,
    period_end_dates=None,
    window=DEFAULT_WINDOW,
    people_per_train=DEFAULT_PEOPLE_PER_TRAIN,
    top_n=DEFAULT_TOP_N,
    generated_at=None,
):
    """Compile the rolling-window summary the webpage reads.

    period_summaries: iterable of per-period dicts from aggregate_rows().
    Returns minutes (not hours); the frontend applies people_per_train / 60.
    """
    period_end_dates = period_end_dates or {}

    # De-dupe by period (latest wins) and order chronologically.
    by_period = {}
    for s in period_summaries:
        if s.get("period"):
            by_period[s["period"]] = s
    ordered = sorted(by_period.values(), key=lambda s: period_sort_key(s["period"]))
    selected = ordered[-window:] if window else ordered

    def end_of(code):
        return period_end_dates.get(code)

    def aggregate(period_list):
        """Roll up a list of per-period summaries into totals, a top-N category
        ranking, and a per-period time series (for the trend chart)."""
        delay = 0
        cancel = 0
        cat_minutes = defaultdict(lambda: {"delay": 0, "cancellation": 0})
        series = []
        for s in period_list:
            d = s.get("delay_minutes", 0)
            c = s.get("cancellation_minutes", 0)
            delay += d
            cancel += c
            for name, v in s.get("categories", {}).items():
                cat_minutes[name]["delay"] += v.get("delay", 0)
                cat_minutes[name]["cancellation"] += v.get("cancellation", 0)
            series.append({
                "period": s["period"],
                "end": end_of(s["period"]),
                "delay_minutes": round(d, 2),
                "cancellation_minutes": round(c, 2),
                "total_minutes": round(d + c, 2),
            })

        top = sorted(
            cat_minutes.items(),
            key=lambda kv: kv[1]["delay"] + kv[1]["cancellation"],
            reverse=True,
        )[:top_n]
        top_categories = [
            {
                "name": name,
                "delay_minutes": round(v["delay"], 2),
                "cancellation_minutes": round(v["cancellation"], 2),
                "minutes": round(v["delay"] + v["cancellation"], 2),
            }
            for name, v in top
        ]
        return {
            "delay_minutes": round(delay, 2),
            "cancellation_minutes": round(cancel, 2),
            "total_minutes": round(delay + cancel, 2),
            "top_categories": top_categories,
            "series": series,
        }

    win = aggregate(selected)
    allt = aggregate(ordered)
    win_codes = [s["period"] for s in selected]
    all_codes = [s["period"] for s in ordered]

    # All-time block (ignores the rolling window) powering the "all historic
    # hours" toggle — now carries its own breakdown and series too.
    all_time = {
        "window_periods": len(ordered),
        "period_first": all_codes[0] if all_codes else None,
        "period_last": all_codes[-1] if all_codes else None,
        "coverage_start": end_of(all_codes[0]) if all_codes else None,
        "coverage_end": end_of(all_codes[-1]) if all_codes else None,
        **allt,
    }

    return {
        "schema_version": 2,
        "generated_at": generated_at,
        "people_per_train": people_per_train,
        "window_periods": len(selected),
        "period_first": win_codes[0] if win_codes else None,
        "period_last": win_codes[-1] if win_codes else None,
        "coverage_start": end_of(win_codes[0]) if win_codes else None,
        "coverage_end": end_of(win_codes[-1]) if win_codes else None,
        "periods_included": win_codes,
        **win,
        "all_time": all_time,
    }
