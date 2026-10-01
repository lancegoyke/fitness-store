from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import UpdateView

from store_project.meso.names import clean_name
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
