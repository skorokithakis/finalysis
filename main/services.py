"""Shared helpers for processing transactions."""

import re
from typing import TypeAlias

from main.models import NormalizationRule

# A compiled normalization rule: (compiled search pattern, replacement).
Rule: TypeAlias = tuple[re.Pattern[str], str]

# Collapse runs of whitespace into a single space. This is the final,
# always-applied hygiene step, kept out of the DB rules on purpose.
_WHITESPACE = re.compile(r"\s+")


def load_rules() -> list[Rule]:
    """Load and compile normalization rules once per import run, in order."""
    return [
        (re.compile(rule.search), rule.replace)
        for rule in NormalizationRule.objects.order_by("order", "pk")
    ]


def clean_description(raw: str, rules: list[Rule]) -> str:
    """Apply the DB normalization rules in order, then collapse whitespace."""
    desc = raw
    for pattern, replace in rules:
        desc = pattern.sub(replace, desc)
    return _WHITESPACE.sub(" ", desc).strip()
