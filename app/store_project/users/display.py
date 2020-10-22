def get_email(user):
    """Takes in User object and returns user's email address."""
    if user.email:
        return user.email
    else:
        return None
