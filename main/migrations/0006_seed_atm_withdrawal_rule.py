# Written by hand on 2026-08-02.

from django.db import migrations

# Collapse the per-withdrawal number so all ATM withdrawals share one merchant.
_ATM_SEARCH = r"^(NON-CHASE ATM WITHDRAW)\s+\d+$"
_ATM_REPLACE = r"\1"


def seed_rule(apps, schema_editor) -> None:
    NormalizationRule = apps.get_model("main", "NormalizationRule")
    # get_or_create because the rule was first added by hand through the admin;
    # existing databases already contain it and must not get a duplicate.
    NormalizationRule.objects.get_or_create(
        search=_ATM_SEARCH,
        defaults={"replace": _ATM_REPLACE, "order": 30},
    )


def unseed_rule(apps, schema_editor) -> None:
    NormalizationRule = apps.get_model("main", "NormalizationRule")
    NormalizationRule.objects.filter(search=_ATM_SEARCH).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("main", "0005_alter_normalizationrule_options"),
    ]

    operations = [
        migrations.RunPython(seed_rule, unseed_rule),
    ]
