"""The report views: the index overview and the month page.

The index shows a monthly bar chart (fed through json_script) whose bars
link to the month pages, plus global top-merchants and spending-by-tag
blocks; the month page (/<year>/<month>/) shows a per-day chart whose bars
pin each day's transaction list, plus month-scoped sidebar blocks.

Spending rule: sum of -amount for rows where counts_as_spending is true; the
DB stores amounts signed as in the source CSV, so a spend is a negative
amount. CREDIT rows are not imported as spending and are ignored entirely.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from django.http import Http404
from django.http import HttpRequest
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import escape

from main.models import Merchant
from main.models import Transaction

# A transaction row as the report consumes it: posting date, merchant name
# (already cleaned at import), and spend amount (positive means money spent).
TransactionRow = tuple[date, str, Decimal]

# Keys of the tag-totals dict: a real tag name, or None as the sentinel for
# the untagged bucket. None is used rather than a string because a
# user-created tag literally named "untagged" would silently merge into a
# string-keyed bucket; the "untagged" label is substituted only at render time.
TagKey = None | str

# Per-day data for the month page: the day total and its transactions as
# (merchant_name, amount) pairs, sorted by amount descending.
DayData = tuple[Decimal, list[tuple[str, Decimal]]]


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
) -> list[tuple[str, float, str]]:
    """Chronological (label, total, month_url) triples for the chart.

    Labels use the "Mon YYYY" form (e.g. "Jan 2026"); months without any
    transactions are included with a total of 0 so the x-axis has no holes.
    Totals are floats because Chart.js consumes numbers, not Decimals. The
    month URL is built server-side with reverse so the client never
    string-builds it from the label.
    """
    by_month: dict[tuple[int, int], Decimal] = {}
    for d, _, amount in transactions:
        key = (d.year, d.month)
        by_month[key] = by_month.get(key, Decimal("0")) + amount
    if not by_month:
        return []
    first = min(by_month)
    last = max(by_month)
    series: list[tuple[str, float, str]] = []
    year, month = first
    while (year, month) <= last:
        series.append(
            (
                date(year, month, 1).strftime("%b %Y"),
                float(by_month.get((year, month), Decimal("0"))),
                reverse("main:month", args=(year, month)),
            )
        )
        month += 1
        if month > 12:
            year += 1
            month = 1
    return series


def day_breakdown(transactions: list[TransactionRow]) -> dict[int, DayData]:
    """Group one month's transactions per day, for the month page chart.

    Returns {day_num: (total, [(merchant_name, amount), ...])} with each
    day's transactions sorted by amount descending, so refunds (negative
    amounts) sort last, matching the month page's pinned day lists.
    """
    txns_by_day: dict[int, list[tuple[str, Decimal]]] = {}
    for d, merchant, amount in transactions:
        txns_by_day.setdefault(d.day, []).append((merchant, amount))
    return {
        day_num: (
            # The explicit start keeps the sum Decimal even for a day whose
            # txns list is empty (impossible here, but mypy cannot see that).
            sum((amount for _, amount in txns), Decimal("0")),
            sorted(txns, key=lambda txn: txn[1], reverse=True),
        )
        for day_num, txns in txns_by_day.items()
    }


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
            "chart_data": monthly_series(transactions),
            "sidebar_merchants_html": merchants_html,
            "sidebar_tags_html": tags_html,
        },
    )


def month(request: HttpRequest, year: int, month: int) -> HttpResponse:
    """The month page: per-day chart, click-to-pin day transactions, sidebar.

    The transactions are the same (date, merchant, spend) rows the index
    view builds, filtered to the month; the sidebar blocks reuse the index's
    aggregators, scoped to that slice.
    """
    txns = Transaction.objects.filter(
        date__year=year, date__month=month, counts_as_spending=True
    ).select_related("merchant")
    transactions = [
        # The DB stores amounts signed as in the CSV, so a spend is a negative
        # amount; negate to get the positive spend figure the report displays.
        (t.date, t.merchant.name, -t.amount)
        for t in txns
    ]
    if not transactions:
        # Months without spending (out-of-range months included) are not part
        # of the report, so they 404 instead of rendering an empty page.
        raise Http404(f"No spending in {year}-{month}")

    days = day_breakdown(transactions)
    # Explicit start keeps the sum Decimal; the 404 above guarantees days is
    # non-empty, but mypy cannot see that.
    month_total = sum((total for total, _ in days.values()), Decimal("0"))

    # The chart payload: year/month for the client-side x-axis (every
    # calendar day, zero-filled) plus per-day totals, transaction counts,
    # and transaction lists for the click handler. Totals stay Decimal up
    # to this JSON boundary; Chart.js consumes floats.
    payload: dict[str, Any] = {
        "year": year,
        "month": month,
        "days": {
            day_num: {
                "total": float(total),
                "count": len(txns),
                "txns": [[merchant, float(amount)] for merchant, amount in txns],
            }
            for day_num, (total, txns) in days.items()
        },
    }

    # Sidebar blocks, scoped to this month only. Both lists are always
    # non-empty here: the 404 above guarantees at least one transaction.
    merchants_html = render_block_html(
        "Top merchants",
        [(name, count, total) for name, total, count in top_merchants(transactions)],
    )
    tags_html = render_block_html(
        "Spending by tag",
        [
            ("untagged" if tag is None else tag, count, total)
            for tag, total, count in tag_totals(transactions, merchant_tags_map())
        ],
    )

    return render(
        request,
        "month.html",
        {
            "month_name": date(year, month, 1).strftime("%B %Y"),
            "month_total": fmt(month_total),
            "txn_count": len(transactions),
            "chart_data": payload,
            "sidebar_merchants_html": merchants_html,
            "sidebar_tags_html": tags_html,
        },
    )
