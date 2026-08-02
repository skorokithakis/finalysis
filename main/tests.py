import os
import tempfile
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from main.models import Transaction

_CSV_HEADER = "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"


class IndexViewTests(TestCase):
    def setUp(self) -> None:
        Transaction.objects.create(
            date=date(2026, 7, 1),
            description="  SOME MERCHANT  07/13KASSANDRA  ",
            amount=Decimal("-10.50"),
            counts_as_spending=True,
            raw={},
        )
        # Transfers are imported but excluded from spending.
        Transaction.objects.create(
            date=date(2026, 7, 2),
            description="ACCT XFER",
            amount=Decimal("-200.00"),
            counts_as_spending=False,
            raw={},
        )

    def test_index_renders_cleaned_negated_spending(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        # The spend is -(-10.50) and the description is cleaned at read time;
        # the transfer must not appear anywhere in the report.
        self.assertContains(response, "$10.50")
        self.assertContains(response, "SOME MERCHANT")
        self.assertNotContains(response, "ACCT XFER")

    def test_index_escapes_descriptions(self) -> None:
        # The tree HTML is injected with |safe, so descriptions must be
        # escaped in the view, not just in the template.
        Transaction.objects.create(
            date=date(2026, 7, 3),
            description="<script>alert('x')</script>",
            amount=Decimal("-5.00"),
            counts_as_spending=True,
            raw={},
        )
        response = self.client.get("/")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")

    def test_index_empty_db_renders_zero_totals(self) -> None:
        Transaction.objects.all().delete()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        # The empty state: zero totals, a placeholder date range, no tree rows.
        self.assertContains(response, "$0.00")
        self.assertContains(response, "—")


class ImportTransactionsCommandTests(TestCase):
    def test_counts_as_spending_rule(self) -> None:
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
