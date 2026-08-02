"""Import transactions from a Chase activity CSV into Transaction."""

import argparse
import csv
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand

from main.models import Merchant
from main.models import Transaction
from main.services import clean_description
from main.services import load_rules

# Transfers are imported like everything else; they just don't count towards
# spending, mirroring the EXCLUDED_TYPES rule in finalysis.py.
EXCLUDED_TYPES = {"WIRE_OUTGOING", "ACCT_XFER"}


class Command(BaseCommand):
    help = "Import transactions from a Chase activity CSV."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("csv_path", help="Path to the Chase activity CSV.")
        parser.add_argument(
            "--wipe",
            action="store_true",
            help="Delete all existing transactions before importing. Merchants "
            "are kept so their tags survive reimports.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["wipe"]:
            Transaction.objects.all().delete()
        # Rules are read from the DB once per run; recompiling per row would be
        # wasted work on thousands of transactions.
        rules = load_rules()
        transactions: list[Transaction] = []
        with open(options["csv_path"], newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                merchant, _ = Merchant.objects.get_or_create(
                    name=clean_description(row["Description"], rules)
                )
                transactions.append(
                    Transaction(
                        date=datetime.strptime(
                            row["Posting Date"].strip(), "%m/%d/%Y"
                        ).date(),
                        description=row["Description"],
                        merchant=merchant,
                        # Signed exactly as in the CSV, so negative means a spend.
                        amount=Decimal(row["Amount"].strip()),
                        counts_as_spending=(
                            row["Details"].strip() == "DEBIT"
                            and row["Type"].strip() not in EXCLUDED_TYPES
                        ),
                        raw=row,
                    )
                )
        Transaction.objects.bulk_create(transactions)
        self.stdout.write(f"Imported {len(transactions)} transactions.")
