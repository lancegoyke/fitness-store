from django import template

from ..serializers import initials as _initials

register = template.Library()


@register.filter
def initials(name):
    """Two-letter monogram for an avatar ("Maya Okonkwo" → "MO")."""
    return _initials(str(name))
