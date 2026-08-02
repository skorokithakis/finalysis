"""The index view: the spending drill-down report.

Drill-down: year -> quarter -> month -> day (with individual transactions).
Months additionally show a top-10 merchants list above the day rows.

Spending rule: sum of -amount for rows where counts_as_spending is true; the
DB stores amounts signed as in the source CSV, so a spend is a negative
amount. CREDIT rows are not imported as spending and are ignored entirely.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.html import escape

from main.models import Transaction

# Strip trailing currency+amount+rate+EXCHG RTE block.
_FX_TAIL = re.compile(
    r"\s+(?:Euro|Pound\s+Sterl)\s*\d+\.\d+\s+X\s+\d+\.\d+\s+\(EXCHG RTE\)$"
)
# Strip everything from a trailing MM/DD date to end of string
# (handles fused branch names like "07/17KASSANDRE" or "07/13SOFOULI B").
_DATE_TAIL = re.compile(r"\s+\d{2}/\d{2}.*$")
# Collapse runs of whitespace into a single space.
_WHITESPACE = re.compile(r"\s+")

# A transaction row as the report consumes it: posting date, cleaned
# description, and spend amount (positive means money spent).
TransactionRow = tuple[date, str, Decimal]

# The tree nests year -> quarter -> month; a month carries "total", "days",
# and "merchants" sub-dicts (see build_tree).
MonthData = dict[str, Any]
QuarterData = dict[int, MonthData]
Tree = dict[int, dict[str, QuarterData]]


def clean_description(raw: str) -> str:
    """Strip FX tail and trailing date, collapse whitespace."""
    desc = _FX_TAIL.sub("", raw)
    desc = _DATE_TAIL.sub("", desc)
    return _WHITESPACE.sub(" ", desc).strip()


def quarter_label(month: int) -> str:
    """Return Q1..Q4 for a 1-based month number."""
    return f"Q{(month - 1) // 3 + 1}"


def build_tree(transactions: list[TransactionRow]) -> Tree:
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
    tree: Tree = {}
    for d, desc, amount in transactions:
        year = tree.setdefault(d.year, {})
        quarter = year.setdefault(quarter_label(d.month), {})
        month = quarter.setdefault(
            d.month, {"total": Decimal("0"), "days": {}, "merchants": {}}
        )
        month["total"] += amount
        day = month["days"].setdefault(d.day, {"total": Decimal("0"), "txns": []})
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


def _year_totals(tree: Tree) -> dict[int, Decimal]:
    return {
        year: sum(m["total"] for q in quarters.values() for m in q.values())
        for year, quarters in tree.items()
    }


def _quarter_totals(quarters: dict[str, QuarterData]) -> dict[str, Decimal]:
    return {
        q_label: sum(m["total"] for m in months.values())
        for q_label, months in quarters.items()
    }


def _month_totals(months: QuarterData) -> dict[int, Decimal]:
    return {m_num: months[m_num]["total"] for m_num in months}


def render_tree_html(tree: Tree) -> str:
    """Walk the tree and produce nested <details>/<summary> HTML.

    The return value is injected into the template with |safe, so every
    non-literal interpolation (transaction and merchant descriptions) must go
    through escape(); anything that slips through unescaped is an XSS hole.
    """
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
                    max_merch = (
                        max(d["total"] for _, d in top10) if top10 else Decimal("0")
                    )
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
                            f"{m_bar}"
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
                    css_class = (
                        "row row-day refund-row" if d_total < 0 else "row row-day"
                    )
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


def index(request: HttpRequest) -> HttpResponse:
    transactions = [
        # The DB stores amounts signed as in the CSV, so a spend is a negative
        # amount; negate to get the positive spend figure the report displays.
        (t.date, clean_description(t.description), -t.amount)
        for t in Transaction.objects.filter(counts_as_spending=True)
    ]
    tree = build_tree(transactions)

    # Summary header data.
    grand_total = sum(t[2] for t in transactions)
    months_set = {(t[0].year, t[0].month) for t in transactions}
    avg_monthly = grand_total / len(months_set) if months_set else Decimal("0")

    def date_fmt(d: date) -> str:
        return d.strftime("%B %d, %Y")

    if transactions:
        min_date = min(t[0] for t in transactions)
        max_date = max(t[0] for t in transactions)
        date_range = f"{date_fmt(min_date)} – {date_fmt(max_date)}"
    else:
        # min()/max() over an empty list raise ValueError, so fall back to a
        # placeholder range and let the header show zero totals.
        date_range = "—"

    return render(
        request,
        "index.html",
        {
            "grand_total": fmt(grand_total),
            "date_range": date_range,
            "avg_monthly": fmt(avg_monthly),
            "month_count": len(months_set),
            "tree_html": render_tree_html(tree),
        },
    )
