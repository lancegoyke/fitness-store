"""Write only the fields a user changed, so a stale tab can't revert newer saves (#657).

The page renders each field's value a second time as a hidden ``initial_<field>``
input. On POST a field is *untouched* when it is absent or equals its rendered
initial, *applied* when changed, and *conflicting* when changed but the stored
value has also moved on since the page rendered.
"""

from django.utils.html import format_html

PREFIX = "initial_"


def _norm(value):
    # Browsers submit textarea newlines as CRLF; stored values use LF.
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def classify(post, current, fields):
    """Return ``(apply, conflicts)`` field lists for ``post`` against ``current``.

    ``current`` maps each field to its stored value as the page would render it.
    A posted field with no ``initial_`` partner is treated as changed.
    """
    apply, conflicts = [], []
    for field in fields:
        if field not in post:
            continue
        submitted = _norm(post[field])
        if PREFIX + field in post:
            initial = _norm(post[PREFIX + field])
            if submitted == initial:
                continue
            if _norm(current[field]) not in (initial, submitted):
                conflicts.append(field)
                continue
        apply.append(field)
    return apply, conflicts


def conflict_message(label):
    verb = "were" if label.endswith("s") else "was"
    return (
        f"{label} {verb} changed elsewhere since you opened this page"
        " — review and save again"
    )


def _join(labels):
    labels = list(labels)
    if len(labels) < 2:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def partial_save_message(saved, conflicted):
    """The notice for a partly saved form (#680).

    E.g. ``Saved notes. Goals changed elsewhere — review and save again.``

    ``saved`` / ``conflicted`` are field labels; used when some fields saved and
    others conflicted. With nothing saved, use ``conflict_message`` instead.
    """
    refused = _join(label.lower() for label in conflicted)
    return (
        f"Saved {_join(label.lower() for label in saved)}. "
        f"{refused[:1].upper()}{refused[1:]} changed elsewhere"
        " — review and save again."
    )


def rerender_state(post, current, fields, *, shown, initial_from_post=False):
    """The ``(values, initials)`` dicts to re-render a form after a failed save.

    ``shown`` are the fields whose posted text stays in the box; the rest show
    the stored value. Initials follow the stored values (so a re-save goes
    through), or the page's original initials when nothing was saved.
    """
    values = {f: post[f] if f in shown and f in post else current[f] for f in fields}
    initials = {
        f: post[PREFIX + f] if initial_from_post and PREFIX + f in post else current[f]
        for f in fields
    }
    return values, initials


def initial_input(field, value, form=""):
    """Hidden input carrying ``field``'s rendered value (optionally ``form=`` id)."""
    if form:
        return format_html(
            '<input type="hidden" name="{}{}" value="{}" form="{}">',
            PREFIX,
            field,
            value,
            form,
        )
    return format_html(
        '<input type="hidden" name="{}{}" value="{}">', PREFIX, field, value
    )
