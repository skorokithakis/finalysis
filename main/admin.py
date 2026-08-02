from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Transaction
from .models import User

admin.site.register(User, UserAdmin)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["date", "description", "amount", "counts_as_spending"]
    list_filter = ["counts_as_spending", "date"]
    search_fields = ["description"]
