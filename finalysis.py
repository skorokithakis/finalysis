#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["flask"]
# ///
"""Spending drill-down report from Chase activity CSV.

Drill-down: year -> quarter -> month -> day (with individual transactions).
Months additionally show a top-10 merchants list above the day rows.

Spending rule: sum of -Amount for rows where Details == 'DEBIT', excluding
Type in {WIRE_OUTGOING, ACCT_XFER}.  CREDIT rows are ignored entirely.
"""

import csv
import re
import sys
from datetime import date, datetime
from decimal import Decimal

from flask import Flask
from markupsafe import escape

EXCLUDED_TYPES = {"WIRE_OUTGOING", "ACCT_XFER"}

# Strip trailing currency+amount+rate+EXCHG RTE block.
_FX_TAIL = re.compile(
    r"\s+(?:Euro|Pound\s+Sterl)\s*\d+\.\d+\s+X\s+\d+\.\d+\s+\(EXCHG RTE\)$"
)
# Strip everything from a trailing MM/DD date to end of string
# (handles fused branch names like "07/17KASSANDRE" or "07/13SOFOULI B").
_DATE_TAIL = re.compile(r"\s+\d{2}/\d{2}.*$")
# Collapse runs of whitespace into a single space.
_WHITESPACE = re.compile(r"\s+")


def clean_description(raw: str) -> str:
    """Strip FX tail and trailing date, collapse whitespace."""
    desc = _FX_TAIL.sub("", raw)
    desc = _DATE_TAIL.sub("", desc)
    return _WHITESPACE.sub(" ", desc).strip()


def quarter_label(month: int) -> str:
    """Return Q1..Q4 for a 1-based month number."""
    return f"Q{(month - 1) // 3 + 1}"


def parse_csv(path: str) -> list[tuple[date, str, Decimal]]:
    """Read the Chase CSV, apply spending filter, return (date, desc, amount)."""
    transactions: list[tuple[date, str, Decimal]] = []
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row["Details"].strip() != "DEBIT":
                continue
            if row["Type"].strip() in EXCLUDED_TYPES:
                continue
            posting_date = datetime.strptime(
                row["Posting Date"].strip(), "%m/%d/%Y"
            ).date()
            amount = -Decimal(row["Amount"].strip())
            cleaned = clean_description(row["Description"])
            transactions.append((posting_date, cleaned, amount))
    return transactions


def build_tree(
    transactions: list[tuple[date, str, Decimal]],
) -> dict:
    """Group transactions into a nested tree with daily totals and merchant data.

    Returns:
      {year: {quarter_label: {
          month_num: {
              "total": Decimal,
              "days": {
                  day_num: {
                      "total": Decimal,
                      "txns": [(desc, amount), ...],
                  }
              },
              "merchants": {desc: {"total": Decimal, "count": int}},
          }
      }}}
    """
    tree: dict = {}
    for d, desc, amount in transactions:
        year = tree.setdefault(d.year, {})
        quarter = year.setdefault(quarter_label(d.month), {})
        month = quarter.setdefault(
            d.month, {"total": Decimal("0"), "days": {}, "merchants": {}}
        )
        month["total"] += amount
        day = month["days"].setdefault(
            d.day, {"total": Decimal("0"), "txns": []}
        )
        day["total"] += amount
        day["txns"].append((desc, amount))
        m = month["merchants"].setdefault(desc, {"total": Decimal("0"), "count": 0})
        m["total"] += amount
        m["count"] += 1
    return tree


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

def bar_html(total: Decimal, max_total: Decimal) -> str:
    """Return a bar inside a flex-track, or empty string when total <= 0."""
    if max_total <= 0 or total <= 0:
        return ""
    pct = float(total / max_total) * 100
    return (
        f'<span class="bar-track">'
        f'<span class="bar" style="width:{pct:.1f}%;"></span>'
        f"</span>"
    )


def fmt(amount: Decimal) -> str:
    """Format a Decimal as a dollar amount."""
    return f"${amount:,.2f}"


def _year_totals(tree: dict) -> dict[int, Decimal]:
    return {
        year: sum(m["total"] for q in quarters.values() for m in q.values())
        for year, quarters in tree.items()
    }


def _quarter_totals(quarters: dict) -> dict[str, Decimal]:
    return {
        q_label: sum(m["total"] for m in months.values())
        for q_label, months in quarters.items()
    }


def _month_totals(months: dict) -> dict[int, Decimal]:
    return {m_num: months[m_num]["total"] for m_num in months}


def render_tree_html(tree: dict) -> str:
    """Walk the tree and produce nested <details>/<summary> HTML."""
    yt = _year_totals(tree)
    max_year = max(yt.values()) if yt else Decimal("0")

    parts: list[str] = []
    for year in sorted(yt):
        quarters = tree[year]
        bar = bar_html(yt[year], max_year)
        parts.append(
            f'<details style="--depth:0"><summary class="row row-year">'
            f'<span class="row-label">{year}</span>'
            f'<span class="row-amount">{fmt(yt[year])}</span>'
            f"{bar}"
            f"</summary>"
        )

        qt = _quarter_totals(quarters)
        max_quarter = max(qt.values()) if qt else Decimal("0")

        for q_label in sorted(qt, key=lambda x: int(x[1])):
            months = quarters[q_label]
            bar = bar_html(qt[q_label], max_quarter)
            parts.append(
                f'<details style="--depth:1"><summary class="row row-quarter">'
                f'<span class="row-label">{q_label}</span>'
                f'<span class="row-amount">{fmt(qt[q_label])}</span>'
                f"{bar}"
                f"</summary>"
            )

            mt = _month_totals(months)
            max_month = max(mt.values()) if mt else Decimal("0")

            for m_num in sorted(mt):
                m_data = months[m_num]
                month_name = date(2000, m_num, 1).strftime("%B")
                bar = bar_html(mt[m_num], max_month)
                parts.append(
                    f'<details style="--depth:2"><summary class="row row-month">'
                    f'<span class="row-label">{month_name}</span>'
                    f'<span class="row-amount">{fmt(mt[m_num])}</span>'
                    f"{bar}"
                    f"</summary>"
                )

                # --- top merchants for this month (above day rows) ---
                merchants = m_data["merchants"]
                top10 = sorted(
                    merchants.items(), key=lambda kv: kv[1]["total"], reverse=True
                )[:10]
                if top10:
                    max_merch = max(d["total"] for _, d in top10) if top10 else Decimal("0")
                    parts.append('<div class="top-merchants">')
                    parts.append('<div class="top-merchants-title">Top merchants</div>')
                    for desc, data in top10:
                        total = data["total"]
                        count = data["count"]
                        m_bar = bar_html(total, max_merch)
                        parts.append(
                            f'<div class="merchant-row">'
                            f'<span class="merchant-name" title="{escape(desc)}">{escape(desc)}</span>'
                            f'<span class="merchant-count">×{count}</span>'
                            f'<span class="merchant-amount">{fmt(total)}</span>'
                            f'{m_bar}'
                            f"</div>"
                        )
                    parts.append("</div>")

                # --- day rows (expandable, with transactions) ---
                days_dict = m_data["days"]
                max_day = (
                    max(d["total"] for d in days_dict.values())
                    if days_dict
                    else Decimal("0")
                )
                for day_num in sorted(days_dict):
                    day_data = days_dict[day_num]
                    d_total = day_data["total"]
                    bar = bar_html(d_total, max_day)
                    css_class = "row row-day refund-row" if d_total < 0 else "row row-day"
                    parts.append(
                        f'<details style="--depth:3"><summary class="{css_class}">'
                        f'<span class="row-label">{m_num}/{day_num}</span>'
                        f'<span class="row-amount">{fmt(d_total)}</span>'
                        f"{bar}"
                        f"</summary>"
                    )

                    # Transaction list inside the expanded day.
                    if day_data["txns"]:
                        parts.append('<div class="txn-list">')
                        for desc, amt in day_data["txns"]:
                            txn_css = "txn-amount refund" if amt < 0 else "txn-amount"
                            parts.append(
                                f'<div class="txn-row">'
                                f'<span class="txn-desc" title="{escape(desc)}">{escape(desc)}</span>'
                                f'<span class="{txn_css}">{fmt(amt)}</span>'
                                f"</div>"
                            )
                        parts.append("</div>")
                    parts.append("</details>")  # day

                parts.append("</details>")  # month
            parts.append("</details>")  # quarter
        parts.append("</details>")  # year

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Page template
# ---------------------------------------------------------------------------

PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spending report</title>
<link rel="icon" href="data:,">
<style>
* {{ box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    padding: 2em;
    margin: 0;
    font-variant-numeric: tabular-nums;
}}
.card {{
    background: #fff;
    border-radius: 12px;
    padding: 1.5em 2em;
    margin-bottom: 1em;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07);
    /* The card is the grid container for the drilldown rows */
}}
h1 {{
    font-size: 1.35em;
    font-weight: 600;
    margin: 0 0 0.6em;
    color: #111;
}}
.summary-header {{
    display: flex;
    gap: 2.5em;
    flex-wrap: wrap;
    padding-bottom: 0.8em;
    border-bottom: 1px solid #e8eaed;
    margin-bottom: 0.6em;
}}
.summary-item {{
    display: flex;
    flex-direction: column;
    gap: 0.15em;
}}
.summary-label {{
    font-size: 0.75em;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #888;
}}
.summary-value {{
    font-size: 1.15em;
    font-weight: 600;
}}

/* ==== details / summary reset and grid rows ==== */

details {{
    display: block;
    border-left: 1px solid transparent;
    transition: border-color 0.15s;
}}
details[open] {{
    border-left-color: #e5e7eb;
}}
details[open] > details {{
    /* Nested open details also show their border */
    border-left-color: #e5e7eb;
}}

summary {{
    display: grid;
    grid-template-columns: 1.2em 1fr 7em minmax(0, 35%);
    align-items: center;
    gap: 0.4em;
    cursor: pointer;
    padding: 0.25em 0.4em;
    border-radius: 4px;
    list-style: none;
    outline: none;
}}
summary::-webkit-details-marker {{ display: none; }}
summary::before {{
    content: "▸";
    color: #999;
    font-size: 0.8em;
    text-align: center;
    transition: transform 0.15s;
    grid-column: 1;
    grid-row: 1;
}}
details[open] > summary::before {{
    content: "▾";
}}
summary:hover {{ background: #f6f8fb; }}

/* typographic hierarchy */
.row-year .row-label {{ font-size: 1.1em; font-weight: 600; }}
.row-quarter .row-label {{ font-size: 1.05em; font-weight: 600; }}
.row-month .row-label {{ font-weight: 500; }}
.row-day .row-label {{ font-size: 0.93em; color: #444; }}
.refund-row .row-label {{ color: #2e7d32; }}

.row-label {{
    grid-column: 2;
    grid-row: 1;
    padding-left: calc(var(--depth, 0) * 1.5rem);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}
.row-amount {{
    grid-column: 3;
    grid-row: 1;
    text-align: right;
    font-variant-numeric: tabular-nums;
}}
.bar-track {{
    grid-column: 4;
    grid-row: 1;
    height: 0.7em;
    min-width: 0;
    background: #eef1f5;
    border-radius: 4px;
    overflow: hidden;
}}
.bar {{
    display: block;
    height: 100%;
    background: #4a90d9;
    border-radius: 4px;
}}

/* ==== top merchants (inside month, above days) ==== */
.top-merchants {{
    margin: 0.4em 0 0.6em;
    padding: 0.6em 0.8em;
    background: #f8f9fb;
    border-radius: 8px;
    border: 1px solid #edf0f4;
}}
.top-merchants-title {{
    font-size: 0.78em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #999;
    margin-bottom: 0.5em;
}}
.merchant-row {{
    display: grid;
    grid-template-columns: 1fr auto 7em minmax(0, 30%);
    align-items: center;
    gap: 0.4em;
    padding: 0.12em 0.3em;
    font-size: 0.88em;
    border-radius: 3px;
}}
.merchant-row:hover {{ background: #eef1f7; }}
.merchant-name {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
}}
.merchant-count {{
    width: 4ch;
    text-align: center;
    color: #aaa;
    font-size: 0.85em;
    flex-shrink: 0;
}}
.merchant-amount {{
    text-align: right;
    flex-shrink: 0;
}}

/* ==== transaction list inside expanded days ==== */
.txn-list {{
    margin: 0.2em 0 0.4em;
}}
.txn-row {{
    display: grid;
    grid-template-columns: 1fr 7em;
    align-items: baseline;
    gap: 0.4em;
    padding: 0.15em 0.4em;
    font-size: 0.88em;
    color: #555;
    border-radius: 3px;
}}
.txn-row:nth-child(even) {{ background: #fafbfc; }}
.txn-row:hover {{ background: #f0f4ff; }}
.txn-desc {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    padding-left: calc(var(--depth, 0) * 1.5rem + 1.2em);
}}
.txn-amount {{
    text-align: right;
    font-variant-numeric: tabular-nums;
}}
.txn-amount.refund {{ color: #2e7d32; }}
</style>
</head>
<body>
<div class="card">
<h1>Spending report</h1>
<div class="summary-header">
<div class="summary-item">
    <span class="summary-label">Grand total</span>
    <span class="summary-value">{grand_total}</span>
</div>
<div class="summary-item">
    <span class="summary-label">Date range</span>
    <span class="summary-value" style="font-size:0.95em;">{date_range}</span>
</div>
<div class="summary-item">
    <span class="summary-label">Avg / month ({month_count} mo)</span>
    <span class="summary-value">{avg_monthly}</span>
</div>
</div>

{tree_html}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path_to_csv>", file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]
    transactions = parse_csv(csv_path)
    tree = build_tree(transactions)

    # Summary header data.
    grand_total = sum(t[2] for t in transactions)
    min_date = min(t[0] for t in transactions)
    max_date = max(t[0] for t in transactions)
    months_set = {(t[0].year, t[0].month) for t in transactions}
    avg_monthly = grand_total / len(months_set) if months_set else Decimal("0")

    def date_fmt(d: date) -> str:
        return d.strftime("%B %d, %Y")

    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        return PAGE.format(
            grand_total=fmt(grand_total),
            date_range=f"{date_fmt(min_date)} – {date_fmt(max_date)}",
            avg_monthly=fmt(avg_monthly),
            month_count=len(months_set),
            tree_html=render_tree_html(tree),
        )

    app.run(host="127.0.0.1", port=5555, debug=False)


if __name__ == "__main__":
    main()
