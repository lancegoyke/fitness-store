"""Apply the site's form component classes at the widget level.

Replaces ``crispy_bulma``: instead of a template pack that emitted Bulma markup
(against a stylesheet that was never loaded), we tag each widget with our own
``input`` / ``select`` component classes, which ``base.css`` styles. Templates
then render fields with ``_form_fields.html``.
"""

from django import forms

# Widgets that have bespoke styling in base.css and must keep their own look.
_SKIP_WIDGETS = (
    forms.CheckboxInput,
    forms.CheckboxSelectMultiple,
    forms.RadioSelect,
    forms.FileInput,
    forms.HiddenInput,
)


def apply_component_classes(fields):
    """Add ``input`` / ``select`` CSS classes to a form's fields, in place.

    ``fields`` is a form's ``fields`` mapping (e.g. ``form.fields`` or
    ``filterset.form.fields``). Select-style widgets get ``select``; text-like
    widgets get ``input``. Checkboxes, radios, file and hidden inputs are left
    untouched.
    """
    for field in fields.values():
        widget = field.widget
        if isinstance(widget, _SKIP_WIDGETS):
            continue
        css_class = "select" if isinstance(widget, forms.Select) else "input"
        _add_class(widget, css_class)


def _add_class(widget, css_class):
    classes = widget.attrs.get("class", "").split()
    if css_class not in classes:
        classes.append(css_class)
        widget.attrs["class"] = " ".join(classes)
