from django import forms


class ContactForm(forms.Form):
    subject = forms.CharField(required=True)
    user_email = forms.EmailField(required=True)
    message = forms.CharField(widget=forms.Textarea, required=True)
