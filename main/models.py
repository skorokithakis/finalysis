import re

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


class User(AbstractUser):
    pass


class Tag(models.Model):
    name = models.CharField(unique=True)

    def __str__(self) -> str:
        return self.name


class NormalizationRule(models.Model):
    # A Python regex applied to transaction descriptions at import time.
    search = models.CharField(max_length=500)
    # re.sub replacement string; backreferences like \1 are allowed.
    replace = models.CharField(max_length=500, blank=True, default="")
    # Rules apply in ascending order, one after the other.
    order = models.IntegerField()

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self) -> str:
        return self.search

    def clean(self) -> None:
        # Reject bad patterns at admin save time, not mid-import.
        try:
            compiled = re.compile(self.search)
        except re.error as error:
            raise ValidationError(
                {"search": f"Invalid regular expression: {error}"}
            ) from error
        # Dry-run the replacement template too: a broken backreference like
        # \2 with a single-group pattern only fails when sub() actually runs,
        # which would crash mid-import instead of here.
        try:
            compiled.sub(self.replace, "")
        except re.error as error:
            raise ValidationError(
                {"replace": f"Invalid replacement: {error}"}
            ) from error


class Merchant(models.Model):
    # Holds the cleaned description, so it needs the same headroom as
    # Transaction.description.
    name = models.CharField(max_length=500, unique=True)
    tags = models.ManyToManyField(Tag, blank=True)

    def __str__(self) -> str:
        return self.name


class Transaction(models.Model):
    date = models.DateField()
    # Raw, uncleaned description straight from the CSV, so it needs headroom.
    description = models.CharField(max_length=500)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT)
    # Signed as in the source CSV: negative means a spend.
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    counts_as_spending = models.BooleanField()
    raw = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
