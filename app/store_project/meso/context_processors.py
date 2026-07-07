from django.utils.functional import SimpleLazyObject

from . import sandbox
from .models import CoachSubscription


def sandbox_status(request):
    """Whether the current visitor is a throwaway sandbox coach (issue #389).

    Also carries ``trial_days`` (issue #416): the persistent sandbox banner in
    ``_meso_base.html`` renders on every sandbox page, not just one view, so
    the value needs to reach the template the same way ``is_sandbox`` does
    rather than being threaded through every view's ``get_context_data``.
    Cheap either way — no query, just the model constant — so it isn't lazy
    like ``is_sandbox``.

    Lazy: pages that never reference ``is_sandbox`` in their template pay no
    extra query.
    """
    return {
        "is_sandbox": SimpleLazyObject(
            lambda: sandbox.is_sandbox(getattr(request, "user", None))
        ),
        "trial_days": CoachSubscription.TRIAL_DAYS,
    }
