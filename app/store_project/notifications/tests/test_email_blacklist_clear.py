"""Issue #507 part 2 — clearing one ``BlacklistedEmail`` row from the dashboard.

A bounce or complaint auto-blacklists a recipient (django-ses's own signal
handlers); once the underlying problem is fixed (a mailbox is reactivated, a
complaint was a mistake, ...) staff need a one-click way to let SES try that
recipient again. POST-only, staff-gated identically to
``EmailDashboardView``, CSRF-protected (the default for a Django form POST).

Pre-implementation this is RED: ``notifications:email_blacklist_clear`` has no
URL/view yet, so every test fails with ``NoReverseMatch``.
"""

import pytest
from django.urls import reverse
from django_ses.models import BlacklistedEmail

from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _url(pk):
    return reverse("notifications:email_blacklist_clear", args=[pk])


class TestBlacklistClear:
    def test_non_staff_post_is_forbidden_and_row_remains(self, client):
        entry = BlacklistedEmail.objects.create(email="bounced@example.com")
        client.force_login(UserFactory())

        resp = client.post(_url(entry.pk))

        assert resp.status_code == 403
        assert BlacklistedEmail.objects.filter(pk=entry.pk).exists()

    def test_anonymous_post_is_redirected_to_login(self, client):
        entry = BlacklistedEmail.objects.create(email="bounced@example.com")

        resp = client.post(_url(entry.pk))

        assert resp.status_code == 302
        assert "/accounts/login/" in resp["Location"]
        assert BlacklistedEmail.objects.filter(pk=entry.pk).exists()

    def test_staff_post_deletes_the_row_and_redirects(self, client):
        entry = BlacklistedEmail.objects.create(email="bounced@example.com")
        client.force_login(UserFactory(is_staff=True))

        resp = client.post(_url(entry.pk))

        assert resp.status_code == 302
        assert resp["Location"] == reverse("notifications:email_dashboard")
        assert not BlacklistedEmail.objects.filter(pk=entry.pk).exists()

    def test_get_is_not_allowed(self, client):
        entry = BlacklistedEmail.objects.create(email="bounced@example.com")
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(_url(entry.pk))

        assert resp.status_code == 405
        assert BlacklistedEmail.objects.filter(pk=entry.pk).exists()
