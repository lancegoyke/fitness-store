from django.contrib.auth import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

User = get_user_model()


class UserChangeForm(forms.UserChangeForm):
    class Meta(forms.UserChangeForm.Meta):
        model = User


class UserCreationForm(forms.UserCreationForm):
    error_message = forms.UserCreationForm.error_messages.update(
        {
            "duplicate_email": _("This email has already been taken."),
            "duplicate_username": _("This username has already been taken."),
        }
    )

    class Meta(forms.UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "email",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make username field optional
        self.fields["username"].required = False
        self.fields["username"].help_text = _(
            "Leave blank to auto-generate from email address."
        )

    def clean_email(self):
        email = self.cleaned_data["email"]

        try:
            User.objects.get(email=email)
        except User.DoesNotExist:
            return email

        raise ValidationError(self.error_messages["duplicate_email"])

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()

        # If no username provided, return empty string (will be generated in save())
        if not username:
            return ""

        try:
            User.objects.get(username=username)
        except User.DoesNotExist:
            return username

        raise ValidationError(self.error_messages["duplicate_username"])

    def save(self, commit=True):
        user = super().save(commit=False)

        # If no username was provided, generate one from email
        if not user.username and user.email:
            base_username = user.email.split("@")[0]
            username = base_username
            counter = 1

            # Ensure username is unique
            while User.objects.filter(username=username).exists():
                username = f"{base_username}{counter}"
                counter += 1

            user.username = username

        if commit:
            user.save()
        return user
