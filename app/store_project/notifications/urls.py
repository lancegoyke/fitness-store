"""Issue #507 part 2 — the superuser email deliverability dashboard's URLs.

Mounted at ``backside/email/`` in ``config.urls`` (namespace ``notifications``),
placed **before** ``path("backside/", admin.site.urls)`` — the bare admin
mount catches everything else under ``backside/``, so anything more specific
must be registered ahead of it.
"""

from django.urls import path

from .views import EmailDashboardBlacklistClearView
from .views import EmailDashboardView

app_name = "notifications"
urlpatterns = [
    path("", EmailDashboardView.as_view(), name="email_dashboard"),
    path(
        "blacklist/<int:pk>/clear/",
        EmailDashboardBlacklistClearView.as_view(),
        name="email_blacklist_clear",
    ),
]
