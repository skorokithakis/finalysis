from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    pass


class Tag(models.Model):
    name = models.CharField(unique=True)

    def __str__(self) -> str:
        return self.name


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
