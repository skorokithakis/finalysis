"""Shared helpers for processing transactions."""

import re

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
