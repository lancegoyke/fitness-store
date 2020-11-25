from django.conf import settings
from django.contrib import messages
from django.core.mail import BadHeaderError, send_mail
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView

from markdownx.utils import markdownify

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
    if request.method == "GET":
        # Render the form
        form = ContactForm()
    else:
        # Send the email
        form = ContactForm(request.POST)
        if form.is_valid():
            subject = form.cleaned_data["subject"]
            from_email = form.cleaned_data["from_email"]
            message = form.cleaned_data["message"]
            try:
                send_mail(subject, message, from_email, [settings.SERVER_EMAIL])
            except BadHeaderError:
                messages.error(
                    request,
                    "The server couldn't send the email because it found an invalid header.",
                )
                return render(request, "pages/contact.html", {"form": form})
            return redirect("pages:contact_success")
    return render(request, "pages/contact.html", {"form": form})


def contact_success_view(request):
    return render(request, "pages/contact_success.html")


@require_GET
def robots_txt(request):
    lines = [
        "User-Agent: *",
        "Disallow: /backside/",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")
