"""The Meso top bar's identity corner (issue #387).

The coach shell used to render a hard-coded, non-interactive "LG" monogram in
the ``.meso-topnav`` corner — wrong initials for every coach but one (and for
every sandbox demo visitor), and dead UI sitting right under the site nav's
real Account/Logout links. The corner's job is *identity*: show who is signed
in (which the site nav's text links don't) and link to the account page. When
Meso grows its own settings surface, this avatar is its natural entry point.

The athlete PWA and public landing overrides of ``topnav_avatar`` are out of
scope here — they already render the right thing (athlete initials / a Log in
button).
"""

import pytest
from django.urls import reverse

from store_project.meso.models import CoachProfile
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

# The default topnav_avatar block in _meso_base.html — one line, so the whole
# anchor is assertable as a stable snippet.
AVATAR_CLASS = (
    'class="meso-avatar meso-avatar--sm meso-avatar--accent meso-avatar--link"'
)


def make_coach(name="Maya Okonkwo"):
    coach = UserFactory(name=name)
    CoachProfile.objects.create(user=coach)
    return coach


class TestCoachTopnavIdentity:
    def test_avatar_shows_the_signed_in_coachs_real_initials(self, client):
        client.force_login(make_coach(name="Maya Okonkwo"))
        body = client.get(reverse("meso:roster")).content.decode()
        assert AVATAR_CLASS in body
        assert ">MO</a>" in body

    def test_avatar_links_to_the_account_page(self, client):
        client.force_login(make_coach())
        body = client.get(reverse("meso:roster")).content.decode()
        assert f'{AVATAR_CLASS} href="{reverse("users:profile")}"' in body

    def test_avatar_tooltip_names_the_signed_in_coach(self, client):
        client.force_login(make_coach(name="Maya Okonkwo"))
        body = client.get(reverse("meso:roster")).content.decode()
        assert 'title="Signed in as Maya Okonkwo' in body

    def test_hardcoded_lg_monogram_is_gone(self, client):
        client.force_login(make_coach(name="Maya Okonkwo"))
        body = client.get(reverse("meso:roster")).content.decode()
        assert 'title="Coach">LG<' not in body

    def test_sandbox_demo_coach_sees_demo_initials_not_lg(self, client):
        # A sandbox visitor is logged in as a throwaway "Demo Coach" → DC.
        body = client.get(reverse("meso:sandbox_enter"), follow=True).content.decode()
        assert ">DC</a>" in body
        assert 'title="Coach">LG<' not in body


class TestAnonymousDefaultBlock:
    def test_offline_page_renders_no_avatar(self, client):
        # offline.html inherits the default block anonymously (it suppresses
        # the site nav but not the avatar) — an identity chip for nobody, or a
        # link into a login-gated page from the offline shell, would be wrong.
        body = client.get(reverse("meso:offline")).content.decode()
        assert "meso-avatar" not in body
        assert reverse("users:profile") not in body
