from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    pass


class Transaction(models.Model):
    date = models.DateField()
    # Raw, uncleaned description straight from the CSV, so it needs headroom.
    description = models.CharField(max_length=500)
    # Signed as in the source CSV: negative means a spend.
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    counts_as_spending = models.BooleanField()
    raw = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
