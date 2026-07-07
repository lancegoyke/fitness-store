from django import template

from .. import tour as meso_tour
from ..serializers import initials as _initials

register = template.Library()


@register.filter
def initials(name):
    """Two-letter monogram for an avatar ("Maya Okonkwo" → "MO")."""
    return _initials(str(name))


@register.filter
def absolute_uri(path, request):
    """``path`` (typically a ``{% static %}`` URL) made absolute (issue #418).

    Social-share crawlers (OG image, etc.) require an absolute URL — a relative
    ``/static/...`` path renders as a broken preview image in Slack/iMessage/
    Discord. Template variable lookups can't call a method with an argument
    (``request.build_absolute_uri`` alone calls it with none, resolving to the
    *current page's* URL), so this filter does the two-argument call instead.
    """
    return request.build_absolute_uri(path)


@register.simple_tag(takes_context=True)
def meso_tour_config(context):
    """The guided-tour front-end config (issue #430) as a plain ``dict``.

    Deliberately *not* fed from a lazy context var: ``json_script`` hands its
    value straight to ``json.dumps``, whose C-accelerated encoder type-checks
    with a raw ``PyDict_Check`` — it never sees through a ``SimpleLazyObject``
    wrapping a dict, so that would raise "not JSON serializable". Cheap to
    call eagerly here because ``_tour.html`` only calls this tag inside its
    ``{% if show_meso_tour %}`` guard — never on a page where the tour is
    hidden.
    """
    return meso_tour.build_config(context["request"].user)
