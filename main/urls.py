"""The main application's URLs."""

from django.urls import path

from . import views

app_name = "main"
urlpatterns = [
    path("", views.index, name="index"),
    # The <int:> converter accepts zero-padded months (/2026/07/) and plain
    # ints (/2026/7/) alike; both resolve to the same view.
    path("<int:year>/<int:month>/", views.month, name="month"),
]
