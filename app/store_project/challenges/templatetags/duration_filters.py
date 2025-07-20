from django import template
from datetime import timedelta

register = template.Library()


@register.filter
def duration_humanize(value):
    """Convert a timedelta object to a human-readable format like '21m 7s'."""
    if not isinstance(value, timedelta):
        return value

    total_seconds = int(value.total_seconds())

    seconds = total_seconds % 60

    parts = []
    if hours := total_seconds // 3600:
        parts.append(f"{hours}h")
    if minutes := (total_seconds % 3600) // 60:
        parts.append(f"{minutes}m")
    if (
        seconds or not parts
    ):  # Show seconds if it's the only unit or if there are remaining seconds
        parts.append(f"{seconds}s")

    return " ".join(parts)
