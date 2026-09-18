from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django_ses.views import SESEventWebhookView
from store_project.exercises.sitemaps import ExerciseSitemap
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
    # notifications for the "Tracking" configuration set. Mounted in every
    # environment (not just PRODUCTION) — SNS signature verification stays on
    # by default (`AWS_SES_VERIFY_EVENT_SIGNATURES`), so the endpoint is safe
    # to expose everywhere, and tests need it reachable too.
    path(
        "ses/events/",
        csrf_exempt(SESEventWebhookView.as_view()),
        name="ses_events",
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
    # Legacy: the old email-only bounce endpoint + the django-ses admin
    # dashboard. Superseded by `ses/events/` above (#507); left in place until
    # the SNS subscription is repointed at the new endpoint and confirmed, then
    # this block gets removed.
    from django_ses.views import handle_bounce

    urlpatterns += [
        path("ses/bounce/", csrf_exempt(handle_bounce)),
        path("backside/django-ses/", include("django_ses.urls")),
    ]
