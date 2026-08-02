import os
import tempfile
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from main.models import Merchant
from main.models import NormalizationRule
from main.models import Tag
from main.models import Transaction
from main.views import build_tree
from main.views import monthly_series
from main.views import tag_totals
from main.views import top_merchants

_CSV_HEADER = "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"


class IndexViewTests(TestCase):
    def setUp(self) -> None:
        merchant = Merchant.objects.create(name="SOME MERCHANT")
        Transaction.objects.create(
            date=date(2026, 7, 1),
            description="  SOME MERCHANT  07/13KASSANDRA  ",
            merchant=merchant,
            amount=Decimal("-10.50"),
            counts_as_spending=True,
            raw={},
        )
        # Transfers are imported but excluded from spending.
        Transaction.objects.create(
            date=date(2026, 7, 2),
            description="ACCT XFER",
            merchant=merchant,
            amount=Decimal("-200.00"),
            counts_as_spending=False,
            raw={},
        )

    def test_index_renders_cleaned_negated_spending(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        # The spend is -(-10.50) and the merchant name (cleaned at import) is
        # shown; the raw description with its date tail must not appear, and
        # the transfer must not appear anywhere in the report.
        self.assertContains(response, "$10.50")
        self.assertContains(response, "SOME MERCHANT")
        self.assertNotContains(response, "KASSANDRA")
        self.assertNotContains(response, "ACCT XFER")
        # The chart data pins the month total too: one month, one (label,
        # total) point serialized via json_script.
        self.assertContains(response, "Jul 2026")
        self.assertContains(response, '"Jul 2026", 10.5')

    def test_index_shows_spending_by_tag(self) -> None:
        tagged = Merchant.objects.create(name="COFFEE SHOP")
        tagged.tags.add(Tag.objects.create(name="Food"))
        Transaction.objects.create(
            date=date(2026, 7, 5),
            description="COFFEE SHOP",
            merchant=tagged,
            amount=Decimal("-4.50"),
            counts_as_spending=True,
            raw={},
        )
        response = self.client.get("/")
        # The month shows a by-tag breakdown; SOME MERCHANT from setUp has no
        # tags, so its spending lands in the untagged bucket.
        self.assertContains(response, "Spending by tag")
        self.assertContains(response, ">Food<")
        self.assertContains(response, ">untagged<")

    def test_index_escapes_merchant_and_tag_names(self) -> None:
        # The tree HTML is injected with |safe, so merchant and tag names must
        # be escaped in the view, not just in the template.
        evil = Merchant.objects.create(name="<b>EVIL</b>")
        evil.tags.add(Tag.objects.create(name="<script>alert('x')</script>"))
        Transaction.objects.create(
            date=date(2026, 7, 3),
            description="raw description",
            merchant=evil,
            amount=Decimal("-5.00"),
            counts_as_spending=True,
            raw={},
        )
        response = self.client.get("/")
        self.assertContains(response, "&lt;b&gt;EVIL&lt;/b&gt;")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<b>EVIL</b>")
        # The raw payload must not appear unescaped. (The page legitimately
        # contains <script> tags for Chart.js and the chart init, so the
        # assertion targets the full payload rather than the tag alone.)
        self.assertNotContains(response, "<script>alert('x')</script>")

    def test_index_empty_db_renders_zero_totals(self) -> None:
        Transaction.objects.all().delete()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        # The empty state: zero totals, a placeholder date range, no tree rows.
        self.assertContains(response, "$0.00")
        self.assertContains(response, "—")


class BuildTreeTests(TestCase):
    def test_day_transactions_sorted_by_descending_amount(self) -> None:
        transactions = [
            (date(2026, 7, 1), "CAFE", Decimal("10.00")),
            (date(2026, 7, 1), "REFUNDS", Decimal("-5.00")),
            (date(2026, 7, 1), "BOOKS", Decimal("4.00")),
            (date(2026, 7, 1), "MARKET", Decimal("12.00")),
        ]
        tree = build_tree(transactions)
        month = tree[2026][7]
        # Total pinning: the month total is the sum of all its transactions.
        self.assertEqual(month["total"], Decimal("21.00"))
        self.assertEqual(month["days"][1]["total"], Decimal("21.00"))
        # Refunds (negative amounts) sort last, after descending spends.
        self.assertEqual(
            month["days"][1]["txns"],
            [
                ("MARKET", Decimal("12.00")),
                ("CAFE", Decimal("10.00")),
                ("BOOKS", Decimal("4.00")),
                ("REFUNDS", Decimal("-5.00")),
            ],
        )

    def test_tag_totals_count_multi_tag_merchants_fully(self) -> None:
        transactions = [
            (date(2026, 7, 1), "CAFE", Decimal("10.00")),
            (date(2026, 7, 2), "CAFE", Decimal("4.00")),
            (date(2026, 7, 3), "BOOKS", Decimal("6.00")),
        ]
        # A merchant with several tags counts fully under each of them, so
        # CAFE's $14 shows up under both tags.
        merchant_tags = {"CAFE": ["Food", "Lunch"]}
        by_tag = tag_totals(transactions, merchant_tags)
        self.assertIn(("Food", Decimal("14.00"), 2), by_tag)
        self.assertIn(("Lunch", Decimal("14.00"), 2), by_tag)
        # BOOKS is absent from the map, so it lands in the untagged bucket,
        # keyed by the None sentinel.
        self.assertIn((None, Decimal("6.00"), 1), by_tag)

    def test_real_untagged_tag_does_not_collide_with_untagged_bucket(self) -> None:
        transactions = [
            (date(2026, 7, 1), "CAFE", Decimal("10.00")),
            (date(2026, 7, 2), "BOOKS", Decimal("6.00")),
        ]
        # A user-created tag named "untagged" must stay its own bucket, kept
        # apart from the None sentinel for merchants without tags.
        merchant_tags = {"CAFE": ["untagged"]}
        by_tag = tag_totals(transactions, merchant_tags)
        self.assertIn(("untagged", Decimal("10.00"), 1), by_tag)
        self.assertIn((None, Decimal("6.00"), 1), by_tag)

    def test_top_merchants_caps_at_15_by_total(self) -> None:
        transactions = [
            (date(2026, 7, 1), f"MERCHANT {i}", Decimal(f"{i + 1}.00"))
            for i in range(20)
        ]
        top = top_merchants(transactions)
        self.assertEqual(len(top), 15)
        # The highest spenders rank first with their totals and counts.
        self.assertEqual(top[0], ("MERCHANT 19", Decimal("20.00"), 1))

    def test_monthly_series_zero_fills_gap_months(self) -> None:
        transactions = [
            (date(2026, 1, 1), "CAFE", Decimal("10.00")),
            (date(2026, 3, 5), "BOOKS", Decimal("6.00")),
        ]
        series = monthly_series(transactions)
        self.assertEqual(
            series,
            [
                ("Jan 2026", 10.0),
                ("Feb 2026", 0.0),
                ("Mar 2026", 6.0),
            ],
        )


class ImportTransactionsCommandTests(TestCase):
    def test_counts_as_spending_rule_and_merchant_linkage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "transactions.csv")
            with open(csv_path, "w", newline="") as handle:
                handle.write(
                    _CSV_HEADER
                    + "DEBIT,07/01/2026,LUNCH,-10.00,DEBIT_CARD,100.00,\n"
                    + "DEBIT,07/02/2026,WIRE OUT,-50.00,WIRE_OUTGOING,50.00,\n"
                    + "CREDIT,07/03/2026,REFUND,20.00,DEBIT_CARD,70.00,\n"
                )
            call_command("import_transactions", csv_path)
        txns = {t.description: t for t in Transaction.objects.all()}
        self.assertTrue(txns["LUNCH"].counts_as_spending)
        self.assertFalse(txns["WIRE OUT"].counts_as_spending)
        self.assertFalse(txns["REFUND"].counts_as_spending)
        # Every row is linked to a merchant named after its cleaned
        # description, credits and excluded types included.
        for txn in txns.values():
            self.assertEqual(txn.merchant.name, txn.description)

    def test_wipe_reimport_keeps_merchant_count_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "transactions.csv")
            with open(csv_path, "w", newline="") as handle:
                handle.write(
                    _CSV_HEADER
                    + 'DEBIT,07/01/2026,"  COFFEE SHOP  07/01KASSANDRA  ",-4.00,DEBIT_CARD,96.00,\n'
                    + "DEBIT,07/02/2026,COFFEE SHOP,-3.00,DEBIT_CARD,93.00,\n"
                )
            call_command("import_transactions", csv_path, wipe=True)
            first_merchant_count = Merchant.objects.count()
            self.assertEqual(Transaction.objects.count(), 2)
            # The second row's description cleans down to the first row's
            # merchant, so the two rows share one merchant.
            self.assertEqual(first_merchant_count, 1)
            self.assertEqual(
                Transaction.objects.filter(merchant__name="COFFEE SHOP").count(), 2
            )
            # A full reimport wipes transactions but not merchants, so the
            # merchant (and its tags, not touched here) survives.
            call_command("import_transactions", csv_path, wipe=True)
            self.assertEqual(Transaction.objects.count(), 2)
            self.assertEqual(Merchant.objects.count(), first_merchant_count)

    def test_seeded_rules_strip_fx_and_date_tails(self) -> None:
        # The seeded rules must produce the same names the hardcoded regexes
        # did: an FX tail (which in the real CSV also carries a date before
        # the FX block, so both rules apply in order) and a fused
        # date/branch tail both clean down to the plain merchant name.
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "transactions.csv")
            with open(csv_path, "w", newline="") as handle:
                handle.write(
                    _CSV_HEADER
                    + 'DEBIT,07/01/2026,"MASOUTIS SIVIRI KASSANDRA  07/29 Euro 13.02 X 1.14 (EXCHG RTE)",-14.85,DEBIT_CARD,85.15,\n'
                    + 'DEBIT,07/02/2026,"  COFFEE SHOP  07/01KASSANDRA  ",-4.00,DEBIT_CARD,96.00,\n'
                )
            call_command("import_transactions", csv_path)
        names = {txn.merchant.name for txn in Transaction.objects.all()}
        self.assertEqual(names, {"MASOUTIS SIVIRI KASSANDRA", "COFFEE SHOP"})

    def test_custom_rule_with_backreference_is_applied(self) -> None:
        NormalizationRule.objects.create(
            order=30,
            search=r"(COFFEE) SHOP",
            replace=r"\1 HOUSE",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "transactions.csv")
            with open(csv_path, "w", newline="") as handle:
                handle.write(
                    _CSV_HEADER
                    + "DEBIT,07/02/2026,COFFEE SHOP,-4.00,DEBIT_CARD,96.00,\n"
                )
            call_command("import_transactions", csv_path)
        txn = Transaction.objects.get()
        self.assertEqual(txn.merchant.name, "COFFEE HOUSE")
