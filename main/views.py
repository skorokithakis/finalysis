"""The index view: the spending drill-down report.

Drill-down: year -> month -> day (with individual transactions). A monthly
bar chart (fed through json_script), a global top-merchants block, and a
global spending-by-tag block sit above the tree.

Spending rule: sum of -amount for rows where counts_as_spending is true; the
DB stores amounts signed as in the source CSV, so a spend is a negative
amount. CREDIT rows are not imported as spending and are ignored entirely.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.html import escape

from main.models import Merchant
from main.models import Transaction

# A transaction row as the report consumes it: posting date, merchant name
# (already cleaned at import), and spend amount (positive means money spent).
TransactionRow = tuple[date, str, Decimal]

# The tree nests year -> month; a month carries "total" and "days" sub-dicts
# (see build_tree).
MonthData = dict[str, Any]
Tree = dict[int, dict[int, MonthData]]

# Keys of the tag-totals dict: a real tag name, or None as the sentinel for
# the untagged bucket. None is used rather than a string because a
# user-created tag literally named "untagged" would silently merge into a
# string-keyed bucket; the "untagged" label is substituted only at render time.
TagKey = None | str


def merchant_tags_map() -> dict[str, list[str]]:
    """Map merchant names to their tag names, in one query over the M2M join.

    Merchants without tags are absent from the map; tag_totals sends them to
    the untagged bucket.
    """
    tags_by_merchant: dict[str, list[str]] = {}
    for merchant_name, tag_name in Merchant.objects.filter(
        tags__isnull=False
    ).values_list("name", "tags__name"):
        tags_by_merchant.setdefault(merchant_name, []).append(tag_name)
    return tags_by_merchant


def build_tree(transactions: list[TransactionRow]) -> Tree:
    """Group transactions into a nested tree with daily totals.

    Returns:
      {year: {month_num: {
          "total": Decimal,
          "days": {
              day_num: {"total": Decimal, "txns": [(merchant_name, amount), ...]},
          },
      }}}

    Each day's transactions are sorted by amount descending, so refunds
    (negative amounts) sort last.
    """
    tree: Tree = {}
    for d, merchant, amount in transactions:
        year = tree.setdefault(d.year, {})
        month = year.setdefault(d.month, {"total": Decimal("0"), "days": {}})
        month["total"] += amount
        day = month["days"].setdefault(d.day, {"total": Decimal("0"), "txns": []})
        day["total"] += amount
        day["txns"].append((merchant, amount))
    for months in tree.values():
        for month in months.values():
            for day in month["days"].values():
                day["txns"].sort(key=lambda txn: txn[1], reverse=True)
    return tree


def top_merchants(
    transactions: list[TransactionRow],
) -> list[tuple[str, Decimal, int]]:
    """The top 15 merchants by total spend, as (name, total, count) tuples.

    Sorted by total descending; the list is capped at 15.
    """
    totals: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for _, merchant, amount in transactions:
        totals[merchant] = totals.get(merchant, Decimal("0")) + amount
        counts[merchant] = counts.get(merchant, 0) + 1
    ranked = sorted(
        ((name, totals[name], counts[name]) for name in totals),
        key=lambda entry: entry[1],
        reverse=True,
    )
    return ranked[:15]


def tag_totals(
    transactions: list[TransactionRow], merchant_tags: dict[str, list[str]]
) -> list[tuple[TagKey, Decimal, int]]:
    """Spending totals per tag over the whole range, most spend first.

    Each transaction counts fully toward every tag of its merchant, and
    toward the untagged bucket when its merchant has no tags, so per-tag
    totals may exceed the grand total; that is accepted. The untagged bucket
    is keyed by the None sentinel.
    """
    totals: dict[TagKey, Decimal] = {}
    counts: dict[TagKey, int] = {}
    for _, merchant, amount in transactions:
        for tag in merchant_tags.get(merchant, [None]):
            totals[tag] = totals.get(tag, Decimal("0")) + amount
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(
        ((tag, totals[tag], counts[tag]) for tag in totals),
        key=lambda entry: entry[1],
        reverse=True,
    )


def monthly_series(
    transactions: list[TransactionRow],
) -> list[tuple[str, float]]:
    """Chronological (label, total) pairs for the chart, gaps zero-filled.

    Labels use the "Mon YYYY" form (e.g. "Jan 2026"); months without any
    transactions are included with a total of 0 so the x-axis has no holes.
    Totals are floats because Chart.js consumes numbers, not Decimals.
    """
    by_month: dict[tuple[int, int], Decimal] = {}
    for d, _, amount in transactions:
        key = (d.year, d.month)
        by_month[key] = by_month.get(key, Decimal("0")) + amount
    if not by_month:
        return []
    first = min(by_month)
    last = max(by_month)
    series: list[tuple[str, float]] = []
    year, month = first
    while (year, month) <= last:
        series.append(
            (
                date(year, month, 1).strftime("%b %Y"),
                float(by_month.get((year, month), Decimal("0"))),
            )
        )
        month += 1
        if month > 12:
            year += 1
            month = 1
    return series


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


def render_block_html(title: str, rows: list[tuple[str, int, Decimal]]) -> str:
    """A sidebar block: title plus (label, count, total) rows.

    The label is user input (a merchant or tag name), so it must go through
    escape() here; the output is injected into the template with |safe, and
    anything that slips through unescaped is an XSS hole.
    """
    max_total = max(total for _, _, total in rows) if rows else Decimal("0")
    parts = [
        '<div class="sidebar-block">',
        f'<div class="sidebar-block-title">{title}</div>',
    ]
    for label, count, total in rows:
        bar = bar_html(total, max_total)
        parts.append(
            f'<div class="merchant-row">'
            f'<span class="merchant-name" title="{escape(label)}">{escape(label)}</span>'
            f'<span class="merchant-count">×{count}</span>'
            f'<span class="merchant-amount">{fmt(total)}</span>'
            f"{bar}"
            f"</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


def _year_totals(tree: Tree) -> dict[int, Decimal]:
    return {
        year: sum(m["total"] for m in months.values()) for year, months in tree.items()
    }


def _month_totals(months: dict[int, MonthData]) -> dict[int, Decimal]:
    return {m_num: months[m_num]["total"] for m_num in months}


def render_tree_html(tree: Tree) -> str:
    """Walk the tree and produce nested <details>/<summary> HTML.

    The return value is injected into the template with |safe, so every
    non-literal interpolation (merchant names) must go through escape();
    anything that slips through unescaped is an XSS hole.
    """
    yt = _year_totals(tree)

    parts: list[str] = []
    for year in sorted(yt):
        # Year rows deliberately have no bar track: the monthly chart above
        # the tree already shows the same trend at a glance.
        parts.append(
            f'<details style="--depth:0"><summary class="row row-year">'
            f'<span class="row-label">{year}</span>'
            f'<span class="row-amount">{fmt(yt[year])}</span>'
            f"</summary>"
        )

        months = tree[year]
        mt = _month_totals(months)
        max_month = max(mt.values()) if mt else Decimal("0")

        for m_num in sorted(mt):
            m_data = months[m_num]
            month_name = date(2000, m_num, 1).strftime("%B")
            bar = bar_html(mt[m_num], max_month)
            parts.append(
                f'<details style="--depth:1"><summary class="row row-month">'
                f'<span class="row-label">{month_name}</span>'
                f'<span class="row-amount">{fmt(mt[m_num])}</span>'
                f"{bar}"
                f"</summary>"
            )

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
                    f'<details style="--depth:2"><summary class="{css_class}">'
                    f'<span class="row-label">{m_num}/{day_num}</span>'
                    f'<span class="row-amount">{fmt(d_total)}</span>'
                    f"{bar}"
                    f"</summary>"
                )

                # Transaction list inside the expanded day.
                if day_data["txns"]:
                    parts.append('<div class="txn-list">')
                    for name, amt in day_data["txns"]:
                        txn_css = "txn-amount refund" if amt < 0 else "txn-amount"
                        parts.append(
                            f'<div class="txn-row">'
                            f'<span class="txn-desc" title="{escape(name)}">{escape(name)}</span>'
                            f'<span class="{txn_css}">{fmt(amt)}</span>'
                            f"</div>"
                        )
                    parts.append("</div>")
                parts.append("</details>")  # day

            parts.append("</details>")  # month
        parts.append("</details>")  # year

    return "\n".join(parts)


def index(request: HttpRequest) -> HttpResponse:
    txns = Transaction.objects.filter(counts_as_spending=True).select_related(
        "merchant"
    )
    transactions = [
        # The DB stores amounts signed as in the CSV, so a spend is a negative
        # amount; negate to get the positive spend figure the report displays.
        # The merchant name is used as-is: it is already cleaned at import, so
        # the report agrees with the merchant table even if cleaning rules
        # change later.
        (t.date, t.merchant.name, -t.amount)
        for t in txns
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

    # Sidebar blocks, global over the whole range. The tag rows resolve the
    # None sentinel to the "untagged" label here, at render time; blocks are
    # omitted entirely when they have nothing to show.
    merchants_html = (
        render_block_html(
            "Top merchants",
            [
                (name, count, total)
                for name, total, count in top_merchants(transactions)
            ],
        )
        if transactions
        else ""
    )
    tags_html = (
        render_block_html(
            "Spending by tag",
            [
                ("untagged" if tag is None else tag, count, total)
                for tag, total, count in tag_totals(transactions, merchant_tags_map())
            ],
        )
        if transactions
        else ""
    )

    return render(
        request,
        "index.html",
        {
            "grand_total": fmt(grand_total),
            "date_range": date_range,
            "avg_monthly": fmt(avg_monthly),
            "month_count": len(months_set),
            "tree_html": render_tree_html(tree),
            "chart_data": monthly_series(transactions),
            "sidebar_merchants_html": merchants_html,
            "sidebar_tags_html": tags_html,
        },
    )
