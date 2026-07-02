from django.utils.functional import SimpleLazyObject

from . import sandbox


def sandbox_status(request):
    """Whether the current visitor is a throwaway sandbox coach (issue #389).

    Lazy: pages that never reference ``is_sandbox`` in their template pay no
    extra query.
    """
    return {
        "is_sandbox": SimpleLazyObject(
            lambda: sandbox.is_sandbox(getattr(request, "user", None))
        )
    }
