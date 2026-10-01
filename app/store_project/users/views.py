from allauth.account.views import SignupView as AllauthSignupView
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import UpdateView

from store_project.meso.claim_session import session_claim_invite
from store_project.meso.names import clean_name
from store_project.meso.names import coach_name
from store_project.products.models import Book
from store_project.products.models import Program

User = get_user_model()


class UserProfileView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "users/profile.html"

    def get_object(self):
        return User.objects.get(email=self.request.user.email)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["programs"] = [
            program
            for program in Program.objects.filter(status=Program.PUBLIC)
            if self.request.user.has_perm(f"products.can_view_{program.slug}")
        ]
        context["books"] = [
            book
            for book in Book.objects.filter(status=Book.PUBLIC)
            if self.request.user.has_perm(f"products.can_view_{book.slug}")
        ]
        return context


user_profile_view = UserProfileView.as_view()


class UserUpdateView(LoginRequiredMixin, UpdateView):
    model = User
    fields = ["name"]
    template_name = "users/profile_update.html"

    def get_success_url(self):
        return reverse("users:profile")

    def get_object(self):
        return User.objects.get(email=self.request.user.email)

    def form_valid(self, form):
        form.instance.name = clean_name(form.cleaned_data["name"])
        messages.add_message(
            self.request, messages.INFO, _("Info successfully updated")
        )
        return super().form_valid(form)


user_update_view = UserUpdateView.as_view()


class SignupView(AllauthSignupView):
    """allauth's signup, aware of an invite the visitor is following (#642).

    Reached from the invite claim page (which flags the session), it prefills the
    name and email the coach supplied (the email stays editable — the claim token,
    not the address, authorises acceptance) and drops the store chrome. A stale
    flag is an ordinary signup page.
    """

    def _claim_invite(self):
        if not hasattr(self, "_claim_invite_cache"):
            self._claim_invite_cache = session_claim_invite(self.request)
        return self._claim_invite_cache

    def get_initial(self):
        initial = super().get_initial()
        invite = self._claim_invite()
        if invite is not None:
            initial["name"] = clean_name(invite.label)
            initial["email"] = invite.email
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        invite = self._claim_invite()
        if invite is not None:
            ctx["claim_invite"] = True
            ctx["claim_coach_name"] = coach_name(invite.coach)
        return ctx


account_signup = SignupView.as_view()
