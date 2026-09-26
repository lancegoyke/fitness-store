def get_display_name(user):
    """Use an account name when set, retaining full-email fallback semantics."""
    return user.name or user.email or None
