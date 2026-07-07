from django.utils.functional import SimpleLazyObject

from . import sandbox
from . import tour
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

    ``show_meso_tour`` (guided-tour Phase 2, issue #430) is the same kind of
    cheap gate for ``_tour.html``: *whether* to render the tour at all, not
    the tour's own content (that's a plain dict built by the
    ``meso_tour_config`` template tag, called only inside the ``{% if
    show_meso_tour %}`` guard — a lazy proxy can't be fed straight into
    ``json_script``, since the C-accelerated JSON encoder only recognizes a
    real ``dict``, not a ``SimpleLazyObject`` wrapping one). Phase 2 is
    sandbox-only, so this short-circuits on ``is_sandbox`` before ever
    touching ``tour_state`` — a real coach's page still does zero extra
    queries.
    """
    user = getattr(request, "user", None)
    is_sandbox_lazy = SimpleLazyObject(lambda: sandbox.is_sandbox(user))
    return {
        "is_sandbox": is_sandbox_lazy,
        "trial_days": CoachSubscription.TRIAL_DAYS,
        "show_meso_tour": SimpleLazyObject(
            lambda: bool(is_sandbox_lazy) and tour.is_active(user)
        ),
    }
