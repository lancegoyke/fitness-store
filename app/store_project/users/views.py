from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import UpdateView
from store_project.products.models import Book
from store_project.products.models import Program

User = get_user_model()


class UserProfileView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "users/profile.html"
    extra_context = {
        "programs": Program.objects.filter(status=Book.PUBLIC),
        "books": Book.objects.filter(status=Book.PUBLIC),
    }

    def get_object(self):
        return User.objects.get(email=self.request.user.email)


user_profile_view = UserProfileView.as_view()


class UserUpdateView(LoginRequiredMixin, UpdateView):
    model = User
    fields = [
        "name",
        "email",
    ]
    template_name = "users/profile_update.html"

    def get_success_url(self):
        return reverse("users:profile")

    def get_object(self):
        return User.objects.get(email=self.request.user.email)

    def form_valid(self, form):
        messages.add_message(
            self.request, messages.INFO, _("Info successfully updated")
        )
        return super().form_valid(form)


user_update_view = UserUpdateView.as_view()
