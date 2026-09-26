"""Resolve the names people see across Meso.

The precedence is deliberately asymmetric. A coach's profile display name is
their own choice about how athletes address them, so it wins over the account
name. A coach's label for an athlete is someone else's guess, so it yields to
the athlete's own account name and is only a fallback before the email prefix.

This module intentionally has no model imports so notification code can use it
without introducing app-loading dependencies.
"""


def clean_name(raw) -> str:
    """Collapse all whitespace in a user-supplied name or label."""
    return " ".join(str(raw or "").split())


def coach_name(user) -> str:
    """Return the coach-controlled display name, then the account fallback."""
    profile = getattr(user, "coach_profile", None)
    profile_name = clean_name(getattr(profile, "display_name", ""))
    return profile_name or clean_name(user.display_name())


def athlete_name(user, label="") -> str:
    """Return the athlete's own name, then this coach's label, then email stem."""
    return (
        clean_name(getattr(user, "name", ""))
        or clean_name(label)
        or clean_name(user.display_name())
    )


def link_athlete_name(link) -> str:
    """Resolve an athlete name from a coach-athlete relationship."""
    return athlete_name(link.athlete, link.label)
