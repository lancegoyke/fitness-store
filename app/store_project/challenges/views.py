from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView
from django.views.generic import DetailView
from django.views.generic import FormView
from django.views.generic.detail import SingleObjectMixin
from taggit.models import Tag

from .filters import ChallengeFilter
from .filters import RecordFilter
from .forms import ChallengeCreateForm
from .forms import RecordCreateForm
from .models import Challenge


@login_required()
def challenge_filtered_list(request, slug=None):
    context = {"tag_list": Tag.objects.all()}

    # Get base queryset - ordering will be handled by the filter
    if slug is not None:
        # use the tag in the URL to filter challenges
        queryset = Challenge.objects.filter(tags__slug__in=[slug])
    else:
        # use all challenges
        queryset = Challenge.objects.all()

    # Apply filters first
    filter_obj = ChallengeFilter(request.GET, queryset=queryset)

    # Group the filtered challenges using our queryset method
    context["filter"] = filter_obj
    context["grouped_challenges"] = filter_obj.qs.grouped()
    return render(request, "challenges/challenge_filtered_list.html", context)


class ChallengeCreateView(PermissionRequiredMixin, CreateView):
    model = Challenge
    form_class = ChallengeCreateForm
    template_name = "challenges/challenge_create.html"
    permission_required = ("challenges.can_edit",)
    raise_exception = False

    def handle_no_permission(self):
        messages.error(self.request, "You don't have permission to create a challenge.")
        return super(ChallengeCreateView, self).handle_no_permission()


class ChallengeDisplay(DetailView):
    model = Challenge

    # make sure the RecordCreateForm is available in the template
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["record_create_form"] = RecordCreateForm()
        records = (
            self.get_object().records.select_related("user").order_by("-date_recorded")
        )
        context["filter"] = RecordFilter(self.request.GET, queryset=records)

        paginator = Paginator(context["filter"].qs, 50)
        page_number = self.request.GET.get("page")
        page_obj = paginator.get_page(page_number)
        context["page_obj"] = page_obj
        context["is_paginated"] = page_obj.has_other_pages()

        query_params = self.request.GET.copy()
        if "page" in query_params:
            query_params.pop("page")
        context["querystring"] = query_params.urlencode()

        # Use the same base queryset for stats calculation
        challenge_records = records  # Use the same queryset with select_related
        if challenge_records.exists():
            context["top_score"] = (
                challenge_records.order_by("time_score").first().time_score
            )
            user_records = challenge_records.filter(user=self.request.user)
            if user_records.exists():
                context["user_pr"] = (
                    user_records.order_by("time_score").first().time_score
                )
        return context


class RecordCreate(SingleObjectMixin, FormView):
    template_name = "challenges/challenge_detail.html"
    form_class = RecordCreateForm  # FormView
    model = Challenge  # SingleObjectMixin

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseForbidden()
        self.object = self.get_object()
        return super().post(request, *args, **kwargs)

    # redirect to challenge_detail page after adding new record
    def get_success_url(self):
        return reverse("challenge_detail", kwargs={"slug": self.object.slug})

    def form_valid(self, form):
        # assign current user to the score being recorded
        form.instance.user = self.request.user
        # assign current challenge from SingleObjectMixin to score being
        # recorded
        form.instance.challenge = self.get_object()
        # don't forget to save!
        form.save()
        return super(RecordCreate, self).form_valid(form)


class ChallengeDetail(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        view = ChallengeDisplay.as_view()
        return view(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        view = RecordCreate.as_view()
        return view(request, *args, **kwargs)
