from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.views.decorators.csrf import csrf_exempt

from config.sitemaps import StaticViewSitemap
from store_project.exercises.sitemaps import ExerciseSitemap
from store_project.pages.sitemaps import PageSitemap
from store_project.products.sitemaps import BookSitemap, ProgramSitemap


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
    path("markdownx/", include("markdownx.urls")),
    path("backside/clearcache/", include("clearcache.urls")),
    path("backside/", admin.site.urls),
    path("cardio/", include("store_project.cardio.urls")),
    path("exercises/", include("store_project.exercises.urls")),
    path("payments/", include("store_project.payments.urls")),
    path("users/", include("store_project.users.urls")),
    path("feed/", include("store_project.feed.urls")),
    path("accounts/", include("allauth.urls")),
    path("", include("store_project.products.urls")),
    path("", include("store_project.pages.urls")),
]

if settings.ENVIRONMENT == "DEVELOPMENT":
    import debug_toolbar

    urlpatterns += [
        path("__debug__/", include(debug_toolbar.urls)),
    ] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.ENVIRONMENT == "PRODUCTION":
    from django_ses.views import handle_bounce

    urlpatterns += [
        path("ses/bounce/", csrf_exempt(handle_bounce)),
        path("backside/django-ses/", include("django_ses.urls")),
        path("admin/", include("admin_honeypot.urls", namespace="admin_honeypot")),
    ]
