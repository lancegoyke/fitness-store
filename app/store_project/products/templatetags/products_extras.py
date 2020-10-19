from django import template


register = template.Library()


@register.filter
def concat(arg1, arg2):
    """
    Concatenate two strings in template.
    """
    return str(arg1) + str(arg2)
