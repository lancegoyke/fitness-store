import os

from django.conf import settings
from django.contrib import messages
from django.core.mail import BadHeaderError, EmailMessage
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView

from markdownx.utils import markdownify
import requests

from store_project.notifications.emails import send_contact_emails
from store_project.pages.forms import ContactForm
from store_project.pages.models import Page


class HomePageView(TemplateView):
    template_name = "pages/home.html"


class SinglePageView(DetailView):
    model = Page
    context_object_name = "page"
    template_name = "pages/single.html"

    def get_context_data(self, **kwargs):
        context = super(SinglePageView, self).get_context_data(**kwargs)
        context["content"] = markdownify(self.object.content)
        return context


def contact_view(request):
    G_RECAPTCHA_SITE_KEY = os.environ.get("G_RECAPTCHA_SITE_KEY")
    G_RECAPTCHA_SECRET_KEY = os.environ.get("G_RECAPTCHA_SECRET_KEY")
    G_RECAPTCHA_ENDPOINT = os.environ.get("G_RECAPTCHA_ENDPOINT")
    if request.method == "GET":
        # Render the form
        form = ContactForm()
    else:
        form = ContactForm(request.POST)
        if form.is_valid():
            # Check if they are a bot
            g_recaptcha_token = request.POST.get("g-recaptcha-response")
            data = {
                "secret": G_RECAPTCHA_SECRET_KEY,
                "response": g_recaptcha_token,
            }
            g_recaptcha_response = requests.post(G_RECAPTCHA_ENDPOINT, data=data).json()
            if g_recaptcha_response["success"] is True:
                # Send the email
                subject = "Mastering Fitness Contact Form: " + form.cleaned_data["subject"]
                message = form.cleaned_data["message"]
                user_email = form.cleaned_data["user_email"]
                try:
                    send_contact_emails(subject, message, user_email)
                    messages.success(
                        request,
                        "Your message was sent! Thanks for the feedback. We emailed you a copy for your records. If needed, someone from our team will reach out to you."
                    )
                except BadHeaderError:
                    messages.error(
                        request,
                        "The server couldn't send the email because it found an invalid header.",
                    )
            elif g_recaptcha_response["error-codes"]:
                for error in g_recaptcha_response["error-codes"]:
                    messages.error(request, f"Something went wrong with Google reCAPTCHA ({error})")
            else:
                messages.error(request, "Google reCAPTCHA said you were a bot! If you're not, maybe try again? Or email me directly: lance [at] lancegoyke [dot] com")
        else:
            messages.error(request, "Sorry, the form you filled out was invalid. Maybe try again?")

    return render(request, "pages/contact.html", {"form": form, "G_RECAPTCHA_SITE_KEY": G_RECAPTCHA_SITE_KEY})


@require_GET
def robots_txt(request):
    lines = [
        "User-Agent: *",
        "Disallow: /backside/",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")
