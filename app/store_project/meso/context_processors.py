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

    ``show_meso_tour`` (guided-tour Phase 2 sandbox + Phase 3 real coach,
    issue #430) is the same kind of cheap gate for ``_tour.html``: *whether*
    to render the tour at all, not the tour's own content (that's a plain
    dict built by the ``meso_tour_config`` template tag, called only inside
    the ``{% if show_meso_tour %}`` guard — a lazy proxy can't be fed straight
    into ``json_script``, since the C-accelerated JSON encoder only
    recognizes a real ``dict``, not a ``SimpleLazyObject`` wrapping one).

    Two independent ways in (decision 4):

    - **Sandbox**: exactly the Phase 2 rule — ``is_sandbox`` and the tour not
      dismissed/completed (``tour.is_active``, which also treats a
      never-started state as active — fine here, since ``create_sandbox``
      always arms the tour at step 0 immediately).
    - **Real coach**: the tour must be *explicitly* active
      (``tour.is_touring`` — a literal ``status == "active"``, not just "not
      hidden"). A real coach's never-started ``{}`` reads as *not* touring,
      so the tour never self-mounts on them; they opt in via the roster's
      "Start the guided tour" entry card, whose POST writes that explicit
      status.

    Short-circuits on ``is_sandbox`` before ever touching ``tour_state``, and
    the real-coach branch short-circuits on ``is_authenticated`` before
    querying — an anonymous visitor's page still does zero extra queries.
    """
    user = getattr(request, "user", None)
    is_sandbox_lazy = SimpleLazyObject(lambda: sandbox.is_sandbox(user))

    def _show_tour():
        if is_sandbox_lazy:
            return tour.is_active(user)
        return bool(getattr(user, "is_authenticated", False)) and tour.is_touring(user)

    return {
        "is_sandbox": is_sandbox_lazy,
        "trial_days": CoachSubscription.TRIAL_DAYS,
        "show_meso_tour": SimpleLazyObject(_show_tour),
    }
