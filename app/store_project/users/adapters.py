"""Project allauth adapters for account mail and social-profile names.

allauth builds every account email (signup confirmation, password reset,
account notices) through ``DefaultAccountAdapter.render_mail``, which returns
the built ``EmailMessage``/``EmailMultiAlternatives`` before its caller
(``send_mail``) calls ``.send()`` on it -- the exact hook point
``notifications.emails.tag_kind`` needs. Wired in via ``ACCOUNT_ADAPTER``
(``config.settings.base``), next to the other ``ACCOUNT_*`` settings. The
``SOCIALACCOUNT_ADAPTER`` also carries provider names into ``User.name`` (#622).
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.urls import reverse

from store_project.meso.names import clean_name
from store_project.notifications.emails import tag_kind
from store_project.notifications.models import EmailKind

# Keyed by the *last path segment* of allauth's own ``template_prefix``
# (e.g. ``"account/email/password_reset_key"`` -> ``"password_reset_key"``),
# covering every prefix this project's allauth version (65.18) sends. A
# prefix not listed here -- an account notice (``password_changed``,
# ``email_changed``, ...; gated behind ``ACCOUNT_EMAIL_NOTIFICATIONS`` and
# off by default in this project) or anything a future allauth version adds
# -- falls back to ``EmailKind.ACCOUNT_NOTICE`` in ``kind_for_template_prefix``.
_KIND_BY_TEMPLATE_NAME = {
    "email_confirmation": EmailKind.ACCOUNT_CONFIRMATION,
    "email_confirmation_signup": EmailKind.ACCOUNT_CONFIRMATION,
    "email_confirm": EmailKind.ACCOUNT_CONFIRMATION,
    "password_reset_key": EmailKind.PASSWORD_RESET,
    "password_reset": EmailKind.PASSWORD_RESET,
    "password_reset_code": EmailKind.PASSWORD_RESET,
    "unknown_account": EmailKind.PASSWORD_RESET,
}


def kind_for_template_prefix(template_prefix: str) -> EmailKind:
    """The ``EmailKind`` for an allauth ``render_mail`` ``template_prefix``.

    Args:
        template_prefix: a template path like
            ``"account/email/password_reset_key"``. Only its last segment is
            looked up, so a project template override under a different
            app/directory still maps correctly.

    Returns:
        The mapped ``EmailKind``, or ``EmailKind.ACCOUNT_NOTICE`` when the
        prefix isn't in the table above.
    """
    template_name = template_prefix.rsplit("/", 1)[-1]
    return _KIND_BY_TEMPLATE_NAME.get(template_name, EmailKind.ACCOUNT_NOTICE)


class AccountAdapter(DefaultAccountAdapter):
    """``DefaultAccountAdapter``, tagging every message with its ``EmailKind``."""

    def render_mail(self, template_prefix, email, context, headers=None):
        message = super().render_mail(template_prefix, email, context, headers)
        tag_kind(message, kind_for_template_prefix(template_prefix))
        return message

    def get_login_redirect_url(self, request):
        from store_project.meso.models import CoachAthlete
        from store_project.meso.models import CoachProfile

        if CoachProfile.objects.filter(user=request.user).exists():
            return reverse("meso:roster")
        if CoachAthlete.objects.filter(
            athlete=request.user,
            status__in=[
                CoachAthlete.Status.ACTIVE,
                CoachAthlete.Status.ACCEPTED_WAITING,
            ],
        ).exists():
            return reverse("meso:athlete_home")
        return super().get_login_redirect_url(request)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Populate an empty account name from either provider's profile fields.

    Google supplies only given/family names while Facebook supplies both those
    fields and ``name``. An existing name is never replaced, so a name the user
    typed remains authoritative.
    """

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if not clean_name(user.name):
            first_name = data.get("first_name") or ""
            last_name = data.get("last_name") or ""
            provider_name = clean_name(data.get("name") or f"{first_name} {last_name}")
            max_length = user._meta.get_field("name").max_length
            user.name = provider_name[:max_length]
        return user
