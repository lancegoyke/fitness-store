from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm


class HoneypotLoginForm(AdminAuthenticationForm):
    def clean(self):
        """A replacement login form.

        Always raise the default error message, because we don't
        care what they entered here.
        """
        raise forms.ValidationError(
            self.error_messages["invalid_login"],
            code="invalid_login",
            params={"username": self.username_field.verbose_name},
        )
