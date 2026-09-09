from django.conf import settings
from django.contrib import messages
from django.core.mail import BadHeaderError
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from markdownx.utils import markdownify

from store_project.notifications.emails import send_contact_emails
from store_project.pages import turnstile
from store_project.pages.forms import ContactForm
from store_project.pages.models import Page

# Where to send someone whose message could not go through the form.
EMAIL_FALLBACK = "email me directly: lance [at] lancegoyke [dot] com"

# The acknowledgement a visitor sees on the page. Contact forms that say nothing
# leave people wondering whether the message went anywhere, so this states
# plainly that we have it.
CONTACT_RECEIVED = (
    "Got it! Your message is in our inbox. We sent an acknowledgement to the "
    "address you gave us, and someone will reply if your message needs one."
)

# Same, for when the acknowledgement email itself could not be delivered -- the
# message is still safely received, which is the part that matters.
CONTACT_RECEIVED_NO_EMAIL = (
    "Got it! Your message is in our inbox and someone will reply if it needs "
    "one. We couldn't send an acknowledgement to the address you gave us, so "
    "please double-check it if you're expecting a reply."
)


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


def _turnstile_error_message(result):
    """What to tell a visitor whose submission did not pass the bot check."""
    if result.is_unavailable:
        return (
            "Our bot check is temporarily unavailable, so your message wasn't "
            f"sent. Please try again in a minute, or {EMAIL_FALLBACK}"
        )
    if result.is_misconfigured:
        return (
            "Our bot check isn't working right now, so your message wasn't "
            f"sent. Sorry about that. Please {EMAIL_FALLBACK}"
        )
    return (
        "The bot check didn't pass, so your message wasn't sent. If you're not "
        f"a bot, please try it again, or {EMAIL_FALLBACK}"
    )


def _send_contact_message(request, form):
    """Send a verified contact message, then report the outcome to the visitor."""
    try:
        acknowledged = send_contact_emails(
            form.cleaned_data["subject"],
            form.cleaned_data["message"],
            form.cleaned_data["user_email"],
        )
    except BadHeaderError:
        messages.error(
            request,
            "The server couldn't send the email because it found an invalid header.",  # noqa: E501
        )
        return
    messages.success(
        request, CONTACT_RECEIVED if acknowledged else CONTACT_RECEIVED_NO_EMAIL
    )


def contact_view(request):
    if request.method == "GET":
        # Render the form
        form = ContactForm()
    else:
        form = ContactForm(request.POST)
        if form.is_valid():
            # Check if they are a bot. Cloudflare Turnstile is the only thing
            # standing between this form and the mail path, so a token that
            # cannot be verified never reaches send_contact_emails.
            result = turnstile.verify(
                request.POST.get(turnstile.TOKEN_FIELD, ""),
                remote_ip=turnstile.client_ip(request),
            )
            if result.success:
                _send_contact_message(request, form)
            else:
                messages.error(request, _turnstile_error_message(result))
        else:
            messages.error(
                request, "Sorry, the form you filled out was invalid. Maybe try again?"
            )

    return render(
        request,
        "pages/contact.html",
        {"form": form, "TURNSTILE_SITE_KEY": settings.TURNSTILE_SITE_KEY},
    )


def timer_view(request):
    return render(request, "pages/timer.html", {})


@require_GET
def robots_txt(request):
    lines = [
        "User-Agent: *",
        "Disallow: /backside/",
        # The public sandbox entry (issue #389): a GET that mints DB rows —
        # crawlers must not hit it repeatedly.
        "Disallow: /meso/demo/",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")
