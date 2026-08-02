"""Import transactions from a Chase activity CSV into Transaction."""

import argparse
import csv
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand

from main.models import Transaction

# Transfers are imported like everything else; they just don't count towards
# spending, mirroring the EXCLUDED_TYPES rule in finalysis.py.
EXCLUDED_TYPES = {"WIRE_OUTGOING", "ACCT_XFER"}


class Command(BaseCommand):
    help = "Import transactions from a Chase activity CSV."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("csv_path", help="Path to the Chase activity CSV.")

    def handle(self, *args: Any, **options: Any) -> None:
        transactions: list[Transaction] = []
        with open(options["csv_path"], newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                transactions.append(
                    Transaction(
                        date=datetime.strptime(
                            row["Posting Date"].strip(), "%m/%d/%Y"
                        ).date(),
                        description=row["Description"],
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
