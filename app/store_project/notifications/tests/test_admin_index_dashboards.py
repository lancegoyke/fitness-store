"""Issue #514: the `/backside/` admin home links to superuser-only dashboards.

``templates/admin/index.html`` extends Django's own ``admin/index.html`` (a
same-name override -- Django's template loader resolves it to the *next*
match in the search path, admin's own bundled template) and adds a
"Dashboards" module to the ``content`` block, ahead of the stock app list,
linking to the email deliverability dashboard and the three Meso superuser
dashboards. This proves the project's ``templates/admin/`` override is
actually picked up (there's already a same-pattern override at
``templates/admin/challenges/challenge/change_form.html``), that the links
resolve, and that the module lives inside ``#content-main`` -- not appended
after ``sidebar``'s own ``#content-related`` div, where it would sink below a
long app list (a real P2 review finding, not hypothetical).
"""

import pytest
from django.urls import reverse

from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


class TestDashboardsModule:
    def test_superuser_sees_the_dashboards_module_with_all_four_links(self, client):
        client.force_login(UserFactory(is_staff=True, is_superuser=True))

        body = client.get(reverse("admin:index")).content.decode()

        assert "Dashboards" in body
        assert reverse("notifications:email_dashboard") in body
        assert reverse("meso:usage_dashboard") in body
        assert reverse("meso:tour_funnel") in body
        assert reverse("meso:product_analytics") in body

    def test_staff_non_superuser_does_not_see_the_dashboards_module(self, client):
        client.force_login(UserFactory(is_staff=True, is_superuser=False))

        resp = client.get(reverse("admin:index"))
        body = resp.content.decode()

        assert resp.status_code == 200
        assert 'id="dashboards-module"' not in body
        assert reverse("notifications:email_dashboard") not in body
        assert reverse("meso:usage_dashboard") not in body
        assert reverse("meso:tour_funnel") not in body
        assert reverse("meso:product_analytics") not in body

    def test_dashboards_module_is_inside_content_main_not_the_sidebar(self, client):
        """The module must sit in the main column, ahead of the app list.

        ``admin/base.html``'s ``sidebar`` block closes ``#content-related``
        itself, so appending after ``{{ block.super }}`` there makes the
        module a stray sibling that sinks below a long app list. Overriding
        ``content`` instead and putting the module first inside
        ``#content-main`` keeps it visible regardless of app-list length.
        """
        client.force_login(UserFactory(is_staff=True, is_superuser=True))

        body = client.get(reverse("admin:index")).content.decode()

        content_main_pos = body.index('id="content-main"')
        dashboards_pos = body.index('id="dashboards-module"')
        # The first real app-list entry (admin/app_list.html's per-app
        # module, e.g. `class="app-users module"`) -- distinct from
        # `dashboards-module` -- must come after the Dashboards module.
        app_list_pos = body.index('class="app-', dashboards_pos)
        assert content_main_pos < dashboards_pos < app_list_pos
        assert "recent-actions-module" in body

    def test_admin_index_still_renders_the_stock_app_list(self, client):
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(reverse("admin:index"))

        assert resp.status_code == 200
        assert 'id="site-name"' in resp.content.decode()
