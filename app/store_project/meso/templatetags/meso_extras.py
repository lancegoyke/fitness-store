from django import template

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
