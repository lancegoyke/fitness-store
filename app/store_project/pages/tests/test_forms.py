import pytest

from store_project.pages.forms import ContactForm


pytestmark = pytest.mark.django_db


class TestContactForm:
    def test_is_valid(self):
        valid_form = ContactForm(
            data={
                "subject": "Subject",
                "from_email": "email@example.com",
                "message": "Here is a test message.",
            }
        )
        assert valid_form.is_valid()

    def test_is_invalid_subject(self):
        invalid_subject_form = ContactForm(
            data={
                "subject": "",
                "from_email": "email@example.com",
                "message": "Here is a test message.",
            }
        )
        assert not invalid_subject_form.is_valid()

    def test_is_invalid_fromemail(self):
        invalid_fromemail_form = ContactForm(
            data={
                "subject": "Subject",
                "from_email": "emailexample.com",
                "message": "Here is a test message.",
            }
        )
        assert not invalid_fromemail_form.is_valid()

    def test_is_invalid_message(self):
        invalid_message_form = ContactForm(
            data={
                "subject": "Subject",
                "from_email": "email@example.com",
                "message": "",
            }
        )
        assert not invalid_message_form.is_valid()
