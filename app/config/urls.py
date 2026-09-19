from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from store_project.exercises.sitemaps import ExerciseSitemap
from store_project.notifications.views import ScopedSESEventWebhookView
from store_project.pages.sitemaps import PageSitemap
from store_project.products.sitemaps import BookSitemap
from store_project.products.sitemaps import ProgramSitemap

from config.sitemaps import StaticViewSitemap

sitemaps = {
    "books": BookSitemap,
    "programs": ProgramSitemap,
    "pages": PageSitemap,
    "exercises": ExerciseSitemap,
    "static": StaticViewSitemap,
}

urlpatterns = [
    path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path(
        "admin/",
        include("store_project.admin_honeypot.urls", namespace="admin_honeypot"),
    ),
    path("markdownx/", include("markdownx.urls")),
    path("backside/clearcache/", include("clearcache.urls")),
    # Staff email deliverability dashboard (#507 part 2). Must precede the
    # bare "backside/" admin mount below — that one catches everything else
    # under backside/.
    path(
        "backside/email/",
        include("store_project.notifications.urls", namespace="notifications"),
    ),
    path("backside/", admin.site.urls),
    path("cardio/", include("store_project.cardio.urls")),
    path("meso/", include("store_project.meso.urls", namespace="meso")),
    path(
        "challenges/", include("store_project.challenges.urls", namespace="challenges")
    ),
    path("exercises/", include("store_project.exercises.urls")),
    path("payments/", include("store_project.payments.urls")),
    path("users/", include("store_project.users.urls")),
    path("feed/", include("store_project.feed.urls")),
    path("accounts/", include("allauth.urls")),
    # SES → SNS event webhook (#507): send/delivery/open/click/bounce/complaint
    # notifications for the "Tracking" configuration set, restricted to the
    # topic(s) in `AWS_SES_EVENT_TOPIC_ARNS` by `ScopedSESEventWebhookView`
    # (an empty allow-list rejects every notification). Mounted in every
    # environment (not just PRODUCTION) — SNS signature verification stays on
    # by default (`AWS_SES_VERIFY_EVENT_SIGNATURES`), so the endpoint is safe
    # to expose everywhere, and tests need it reachable too. `ses/bounce/` is
    # the same guarded view under the legacy path SNS was originally
    # subscribed to (its dispatcher understands the old `notificationType`
    # payload shape too); remove once the SNS subscription is repointed at
    # `ses/events/` and confirmed (see docs/deploy-hetzner.md).
    path(
        "ses/events/",
        csrf_exempt(ScopedSESEventWebhookView.as_view()),
        name="ses_events",
    ),
    path(
        "ses/bounce/",
        csrf_exempt(ScopedSESEventWebhookView.as_view()),
        name="ses_bounce",
    ),
    path("", include("store_project.products.urls")),
    path("", include("store_project.pages.urls")),
]

if settings.ENVIRONMENT == "DEVELOPMENT":
    import debug_toolbar

    urlpatterns += [
        path("__debug__/", include(debug_toolbar.urls)),
        path("__reload__/", include("django_browser_reload.urls")),
    ] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.ENVIRONMENT == "PRODUCTION":
    # Legacy: the django-ses admin dashboard (SES send-statistics page).
    # Unrelated to the webhook endpoints above.
    urlpatterns += [
        path("backside/django-ses/", include("django_ses.urls")),
    ]
