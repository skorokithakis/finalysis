from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import Merchant
from .models import Tag
from .models import Transaction
from .models import User

admin.site.register(User, UserAdmin)
admin.site.register(Tag)


class MerchantTagFilter(admin.SimpleListFilter):
    """Filter merchants by whether they have at least one tag."""

    title = "tags"
    parameter_name = "tags"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin
    ) -> list[tuple[str, str]]:
        return [("tagged", "Tagged"), ("untagged", "Untagged")]

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[Merchant]
    ) -> QuerySet[Merchant]:
        if self.value() == "tagged":
            # The M2M join can duplicate rows, so de-duplicate.
            return queryset.filter(tags__isnull=False).distinct()
        if self.value() == "untagged":
            return queryset.filter(tags__isnull=True)
        return queryset


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ["name", "tags_list"]
    filter_horizontal = ["tags"]
    search_fields = ["name"]
    list_filter = [MerchantTagFilter]

    @admin.display(description="Tags")
    def tags_list(self, merchant: Merchant) -> str:
        return ", ".join(tag.name for tag in merchant.tags.all())


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["date", "merchant", "description", "amount", "counts_as_spending"]
    list_filter = ["counts_as_spending", "date"]
    search_fields = ["description", "merchant__name"]
    # A giant dropdown of 1500+ merchants is unusable, so autocomplete it.
    autocomplete_fields = ["merchant"]
