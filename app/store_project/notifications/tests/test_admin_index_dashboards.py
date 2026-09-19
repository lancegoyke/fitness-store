"""Issue #514: the `/backside/` admin home links out to the staff dashboards.

``templates/admin/index.html`` extends Django's own ``admin/index.html`` (a
same-name override -- Django's template loader resolves it to the *next*
match in the search path, admin's own bundled template) and adds a
"Dashboards" module to the ``sidebar`` block linking to the email
deliverability dashboard and the two existing Meso staff dashboards. This
proves the project's ``templates/admin/`` override is actually picked up
(there's already a same-pattern override at
``templates/admin/challenges/challenge/change_form.html``) and that the
links resolve.
"""

import pytest
from django.urls import reverse

from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


class TestDashboardsModule:
    def test_staff_sees_the_dashboards_module_with_all_three_links(self, client):
        client.force_login(UserFactory(is_staff=True))

        body = client.get(reverse("admin:index")).content.decode()

        assert "Dashboards" in body
        assert reverse("notifications:email_dashboard") in body
        assert reverse("meso:usage_dashboard") in body
        assert reverse("meso:tour_funnel") in body

    def test_admin_index_still_renders_the_stock_app_list(self, client):
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(reverse("admin:index"))

        assert resp.status_code == 200
        assert 'id="site-name"' in resp.content.decode()
