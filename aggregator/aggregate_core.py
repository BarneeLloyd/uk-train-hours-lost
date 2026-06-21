"""Core aggregation logic — pure, no cloud dependencies, easy to unit-test.

Given the rows of one Transparency CSV (one railway period), the
INCIDENT_REASON -> category map and the TOC_CODE -> {name, class} operator map,
produce a tiny per-period summary that keeps minutes broken down *by operator*
so the hours conversion can weight each operator individually:

    {
      "period": "2024/25_P11",
      "rows": 455580,
      "delay_minutes": 2542929,          # sum of PFPI_MINUTES (all operators)
      "cancellation_minutes": 1211065,   # sum of NON_PFPI_MINUTES
      "operators": {                     # raw minutes per TOC_CODE
         "HF": {"delay": 86011, "cancellation": 1200}, ...
      },
      "categories": {                    # minutes split passenger/freight
         "Train operations": {"pass_delay": .., "pass_cancel": .., "frt_delay": .., "frt_cancel": ..}
      },
      "unmatched_codes": {"ZZ": 4}       # incident codes not in the glossary
    }

We store raw MINUTES (never the hours result) so the conversion parameters
(per-operator loads, freight equivalent, rolling window) stay configurable at
summary-build time.

## The hours model

Every category of delay is funnelled through one conversion into a single
"hours of human time lost" figure:

    passenger train:  delay_minutes / 60  x  pax_per_train[operator]
    freight train:    delay_minutes / 60  x  freight_equiv_passengers

* Passenger loads are operator-specific (ORR passenger-km / train-km), defaulting
  to a national average when an operator isn't in the table.
* Freight / engineering / light-loco trains carry no passengers, so their delay
  is bridged to equivalent passenger-hours via the DfT TAG value-of-time ratio,
  expressed as a single `freight_equiv_passengers` constant.

The two are commensurable (both "equivalent person-hours"), so they sum to one
total. See aggregator/conversion_params.json for the numbers and provenance.
"""
import re
from collections import Counter, defaultdict

# Default rolling window: 13 four-week railway periods ~= 12 months.
DEFAULT_WINDOW = 13
DEFAULT_TOP_N = 10

# Conversion fallbacks, used only if conversion_params.json doesn't supply them.
DEFAULT_PASSENGER_LOAD = 126   # ORR national average passengers per train
DEFAULT_FREIGHT_EQUIV = 15     # equivalent passengers per freight train (TAG VoT bridge)

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


# --- the conversion: minutes -> equivalent person-hours -----------------------

def operator_class_map(operator_map):
    """operator_map.json ({code: {name, class}}) -> {code: class}."""
    return {k: v.get("class", "passenger") for k, v in (operator_map or {}).items()}


def make_conversion(conversion_params, operator_map):
    """Assemble the conversion dict build_summary expects from the two bundled
    lookups (conversion_params.json + operator_map.json)."""
    conversion_params = conversion_params or {}
    return {
        "default_passenger_load": conversion_params.get(
            "default_passenger_load", DEFAULT_PASSENGER_LOAD),
        "freight_equiv_passengers": conversion_params.get(
            "freight_equiv_passengers", DEFAULT_FREIGHT_EQUIV),
        "operator_load": conversion_params.get("operator_load", {}),
        "operator_load_year": conversion_params.get("operator_load_year"),
        "operator_class": operator_class_map(operator_map),
    }


def load_for(code, conversion):
    """Equivalent persons per train for an operator code. Per-operator passenger
    load if known, else freight equivalent for freight operators, else the
    national passenger average."""
    override = conversion.get("operator_load", {}).get(code)
    if override is not None:
        return override
    if is_freight(code, conversion):
        return conversion.get("freight_equiv_passengers", DEFAULT_FREIGHT_EQUIV)
    return conversion.get("default_passenger_load", DEFAULT_PASSENGER_LOAD)


def is_freight(code, conversion):
    return conversion.get("operator_class", {}).get(code) == "freight"


def aggregate_rows(dict_rows, reason_map, operator_class=None):
    """dict_rows: iterable of dict-like rows (e.g. from csv.DictReader).

    operator_class: TOC_CODE -> "passenger"|"freight" (from operator_map.json).
    Unknown / blank operators are treated as passenger."""
    operator_class = operator_class or {}
    periods = Counter()
    rows = 0
    delay_total = 0.0
    cancel_total = 0.0
    operators = defaultdict(lambda: {"delay": 0.0, "cancellation": 0.0})
    cats = defaultdict(lambda: {
        "pass_delay": 0.0, "pass_cancel": 0.0, "frt_delay": 0.0, "frt_cancel": 0.0,
    })
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

        code = (row.get("TOC_CODE") or "").strip()
        op = operators[code]
        op["delay"] += pfpi
        op["cancellation"] += non_pfpi
        freight = operator_class.get(code, "passenger") == "freight"

        rc = (row.get("INCIDENT_REASON") or "").strip()
        category = reason_map.get(rc)
        if category is None:
            if rc:
                unmatched[rc] += 1
            category = "Unattributed / unknown"
        elif category == "":
            category = "Unattributed / unknown"

        bucket = cats[category]
        if freight:
            bucket["frt_delay"] += pfpi
            bucket["frt_cancel"] += non_pfpi
        else:
            bucket["pass_delay"] += pfpi
            bucket["pass_cancel"] += non_pfpi

    period = periods.most_common(1)[0][0] if periods else None

    return {
        "period": period,
        "rows": rows,
        "delay_minutes": round(delay_total, 2),
        "cancellation_minutes": round(cancel_total, 2),
        "operators": {
            k: {"delay": round(v["delay"], 2), "cancellation": round(v["cancellation"], 2)}
            for k, v in operators.items()
        },
        "categories": {
            k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in cats.items()
        },
        "unmatched_codes": dict(unmatched),
    }


def build_summary(
    period_summaries,
    period_end_dates=None,
    window=DEFAULT_WINDOW,
    conversion=None,
    top_n=DEFAULT_TOP_N,
    generated_at=None,
):
    """Compile the rolling-window summary the webpage reads, in HOURS.

    conversion: {
        "default_passenger_load": int,
        "freight_equiv_passengers": int,
        "operator_load": {TOC_CODE: pax_per_train},
        "operator_class": {TOC_CODE: "passenger"|"freight"},
    }
    """
    period_end_dates = period_end_dates or {}
    conversion = conversion or {}

    # De-dupe by period (latest wins) and order chronologically.
    by_period = {}
    for s in period_summaries:
        if s.get("period"):
            by_period[s["period"]] = s
    ordered = sorted(by_period.values(), key=lambda s: period_sort_key(s["period"]))
    selected = ordered[-window:] if window else ordered

    def end_of(code):
        return period_end_dates.get(code)

    freight_equiv = conversion.get("freight_equiv_passengers", DEFAULT_FREIGHT_EQUIV)

    def aggregate(period_list):
        """Roll up periods into total hours, a passenger/freight split, a top-N
        category ranking (in hours) and a per-period series for the chart."""
        delay_min = cancel_min = 0.0
        pass_min = pass_hours = 0.0
        frt_min = frt_hours = 0.0
        delay_hours = cancel_hours = 0.0
        cat = defaultdict(lambda: {
            "pass_delay": 0.0, "pass_cancel": 0.0, "frt_delay": 0.0, "frt_cancel": 0.0,
        })
        series = []

        for s in period_list:
            delay_min += s.get("delay_minutes", 0)
            cancel_min += s.get("cancellation_minutes", 0)

            p_delay_h = p_cancel_h = 0.0   # this period's hours (exact, per-operator)
            for code, v in s.get("operators", {}).items():
                load = load_for(code, conversion)
                dh = v.get("delay", 0) * load / 60.0
                ch = v.get("cancellation", 0) * load / 60.0
                p_delay_h += dh
                p_cancel_h += ch
                if is_freight(code, conversion):
                    frt_hours += dh + ch
                    frt_min += v.get("delay", 0) + v.get("cancellation", 0)
                else:
                    pass_hours += dh + ch
                    pass_min += v.get("delay", 0) + v.get("cancellation", 0)

            delay_hours += p_delay_h
            cancel_hours += p_cancel_h

            for name, v in s.get("categories", {}).items():
                b = cat[name]
                for kk in b:
                    b[kk] += v.get(kk, 0)

            series.append({
                "period": s["period"],
                "end": end_of(s["period"]),
                "delay_hours": round(p_delay_h, 1),
                "cancellation_hours": round(p_cancel_h, 1),
                "total_hours": round(p_delay_h + p_cancel_h, 1),
            })

        # Category hours: exact per-operator weighting isn't stored per category,
        # so passenger-attributed category minutes use the window's *blended*
        # passenger load (chosen so the categories reconcile with passenger_hours),
        # and freight-attributed minutes use the freight equivalent.
        blended_pass_load = (
            pass_hours * 60.0 / pass_min if pass_min > 0
            else conversion.get("default_passenger_load", DEFAULT_PASSENGER_LOAD)
        )
        cat_list = []
        for name, b in cat.items():
            cdh = (b["pass_delay"] * blended_pass_load + b["frt_delay"] * freight_equiv) / 60.0
            cch = (b["pass_cancel"] * blended_pass_load + b["frt_cancel"] * freight_equiv) / 60.0
            cat_list.append({
                "name": name,
                "delay_hours": round(cdh, 1),
                "cancellation_hours": round(cch, 1),
                "hours": round(cdh + cch, 1),
            })
        cat_list.sort(key=lambda x: x["hours"], reverse=True)

        return {
            "delay_hours": round(delay_hours, 1),
            "cancellation_hours": round(cancel_hours, 1),
            "total_hours": round(delay_hours + cancel_hours, 1),
            "passenger_hours": round(pass_hours, 1),
            "freight_hours": round(frt_hours, 1),
            "delay_minutes": round(delay_min, 2),
            "cancellation_minutes": round(cancel_min, 2),
            "total_minutes": round(delay_min + cancel_min, 2),
            "top_categories": cat_list[:top_n],
            "series": series,
        }

    win = aggregate(selected)
    allt = aggregate(ordered)
    win_codes = [s["period"] for s in selected]
    all_codes = [s["period"] for s in ordered]

    all_time = {
        "window_periods": len(ordered),
        "period_first": all_codes[0] if all_codes else None,
        "period_last": all_codes[-1] if all_codes else None,
        "coverage_start": end_of(all_codes[0]) if all_codes else None,
        "coverage_end": end_of(all_codes[-1]) if all_codes else None,
        **allt,
    }

    return {
        "schema_version": 3,
        "generated_at": generated_at,
        # Methodology metadata for the page's limitations note.
        "default_passenger_load": conversion.get("default_passenger_load", DEFAULT_PASSENGER_LOAD),
        "freight_equiv_passengers": freight_equiv,
        "operator_load_year": conversion.get("operator_load_year"),
        "window_periods": len(selected),
        "period_first": win_codes[0] if win_codes else None,
        "period_last": win_codes[-1] if win_codes else None,
        "coverage_start": end_of(win_codes[0]) if win_codes else None,
        "coverage_end": end_of(win_codes[-1]) if win_codes else None,
        "periods_included": win_codes,
        **win,
        "all_time": all_time,
    }
