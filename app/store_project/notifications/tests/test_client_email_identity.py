"""#639: client-facing emails come from the coach, replies go to the coach."""

from email.header import decode_header
from email.header import make_header
from email.utils import parseaddr

import pytest
from django.conf import settings
from django.core import mail

from store_project.notifications.emails import client_email_identity
from store_project.notifications.emails import first_name
from store_project.notifications.emails import send_athlete_waiting_email
from store_project.notifications.emails import send_block_delivered_email
from store_project.notifications.emails import send_coach_invite_email
from store_project.notifications.emails import send_coach_invite_reminder_email
from store_project.notifications.emails import send_coach_request_email
from store_project.notifications.emails import send_contact_emails
from store_project.notifications.emails import send_invite_accepted_email
from store_project.notifications.emails import send_relationship_ended_email
from store_project.notifications.emails import send_relationship_restored_email
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "https://x.test/meso/"
PLAN = type("Plan", (), {"title": "Hypertrophy Block"})()


def _address():
    return parseaddr(settings.DEFAULT_FROM_EMAIL)[1]


def _coach(**kwargs):
    kwargs.setdefault("name", "Sam Rivera")
    kwargs.setdefault("email", "sam@coach.test")
    return UserFactory(**kwargs)


def _invite(coach, **kwargs):
    return send_coach_invite_email(
        coach=coach, email="jordan@example.com", accept_url=URL, **kwargs
    )


def _reminder(coach, **kwargs):
    return send_coach_invite_reminder_email(
        coach=coach, email="jordan@example.com", accept_url=URL, **kwargs
    )


def _delivered(coach, athlete=None, **kwargs):
    athlete = athlete or UserFactory(name="Jordan Ellis")
    return send_block_delivered_email(
        athlete=athlete,
        coach=coach,
        plan=PLAN,
        week_count=2,
        home_url=URL,
        **kwargs,
    )


def _ended(coach, athlete=None, **kwargs):
    athlete = athlete or UserFactory(name="Jordan Ellis")
    return send_relationship_ended_email(
        athlete=athlete, coach=coach, home_url=URL, **kwargs
    )


def _restored(coach, athlete=None, **kwargs):
    athlete = athlete or UserFactory(name="Jordan Ellis")
    return send_relationship_restored_email(
        athlete=athlete,
        coach=coach,
        home_url=URL,
        restored_plan_titles=["Strength Foundations"],
        **kwargs,
    )


SENDERS = [_invite, _reminder, _delivered, _ended, _restored]


@pytest.mark.parametrize("send", SENDERS)
def test_client_email_comes_from_the_coach_and_replies_to_them(send):
    coach = _coach()

    send(coach)

    message = mail.outbox[0]
    name, address = parseaddr(message.message()["From"])
    assert name == "Sam Rivera via Mastering Fitness"
    assert address == _address()
    assert message.reply_to == ["sam@coach.test"]
    assert parseaddr(message.message()["Reply-To"])[1] == "sam@coach.test"


@pytest.mark.parametrize("send", SENDERS)
def test_coach_without_email_gets_no_reply_to(send):
    coach = _coach(email="")

    send(coach)

    message = mail.outbox[0]
    assert message.reply_to == []
    assert message.message()["Reply-To"] is None


@pytest.mark.parametrize("send", SENDERS)
def test_tricky_coach_name_cannot_inject_headers(send):
    coach = _coach(name='Sam, "The Tank" Ríos\r\nBcc: evil@x.test')

    send(coach)

    message = mail.outbox[0]
    raw = message.message()
    assert raw["Bcc"] is None
    assert "\n" not in str(raw["From"])
    name, address = parseaddr(str(raw["From"]))
    assert address == _address()
    assert name.endswith("via Mastering Fitness")
    assert message.bcc == []


def test_identity_helper_quotes_commas_and_encodes_non_ascii():
    coach = _coach(name='Sam, "The Tank" Ríos')

    identity = client_email_identity(coach)

    name, address = parseaddr(identity["from_email"])
    name = str(make_header(decode_header(name)))
    assert name == "Sam, The Tank Ríos via Mastering Fitness"  # quotes stripped
    assert address == _address()
    assert identity["reply_to"] == ["sam@coach.test"]


class TestGreeting:
    def test_first_name_helper(self):
        assert first_name("Jordan Ellis") == "Jordan"
        assert first_name("  Jordan   ") == "Jordan"
        assert first_name("") == ""
        assert first_name(None) == ""

    def test_invite_greets_by_first_name(self):
        _invite(_coach(), recipient_name="Jordan Ellis")

        message = mail.outbox[0]
        assert message.body.startswith("Hi Jordan,")
        assert "Hi Jordan," in message.alternatives[0][0]
        assert "Jordan Ellis" not in message.body.split("\n")[0]

    def test_invite_without_name_is_plain_hi(self):
        _invite(_coach())

        assert mail.outbox[0].body.startswith("Hi,")
        assert "<p>Hi,</p>" in mail.outbox[0].alternatives[0][0]

    def test_reminder_greets_by_first_name(self):
        _reminder(_coach(), recipient_name="Jordan Ellis")

        message = mail.outbox[0]
        assert message.body.startswith("Hi Jordan,")
        assert "Hi Jordan," in message.alternatives[0][0]

    def test_reminder_without_name_is_plain_hi(self):
        _reminder(_coach())

        assert mail.outbox[0].body.startswith("Hi,")

    @pytest.mark.parametrize("send", [_delivered, _ended, _restored])
    def test_delivery_and_ended_greet_by_first_name(self, send):
        send(_coach())

        message = mail.outbox[0]
        assert message.body.startswith("Hi Jordan,")
        assert "Hi Jordan," in message.alternatives[0][0]
        assert "Hi Jordan Ellis" not in message.body

    @pytest.mark.parametrize("send", [_delivered, _ended, _restored])
    def test_label_is_used_when_the_athlete_has_no_name(self, send):
        athlete = UserFactory(name="", email="jordan.ellis@example.com")

        send(_coach(), athlete=athlete, athlete_label="Jordan Ellis")

        assert mail.outbox[0].body.startswith("Hi Jordan,")

    @pytest.mark.parametrize("send", [_delivered, _ended, _restored])
    def test_no_name_at_all_is_plain_hi_not_email_stem(self, send):
        athlete = UserFactory(name="", email="jordan.ellis@example.com")

        send(_coach(), athlete=athlete)

        body = mail.outbox[0].body
        assert body.startswith("Hi,")
        assert "Hi jordan" not in body
        assert "Hi Your" not in body


def _coach_request(coach):
    return send_coach_request_email(
        athlete=UserFactory(name="Jordan Ellis"), coach=coach, roster_url=URL
    )


def _athlete_waiting(coach):
    return send_athlete_waiting_email(
        athlete=UserFactory(name="Jordan Ellis"), coach=coach, roster_url=URL
    )


def _invite_accepted(coach):
    return send_invite_accepted_email(
        athlete=UserFactory(name="Jordan Ellis"), coach=coach, roster_url=URL
    )


COACH_FACING = [_coach_request, _athlete_waiting, _invite_accepted]


class TestCoachFacingGreeting:
    """#688.4: mail TO the coach greets them by first name."""

    @pytest.mark.parametrize("send", COACH_FACING)
    def test_named_coach_is_greeted_by_first_name(self, send):
        send(_coach(name="Sam Rivera"))

        message = mail.outbox[0]
        assert message.body.startswith("Hi Sam,")
        assert "<p>Hi Sam,</p>" in message.alternatives[0][0]
        assert "Hi Sam Rivera" not in message.body

    @pytest.mark.parametrize("send", COACH_FACING)
    def test_unnamed_coach_is_plain_hi_not_email_stem(self, send):
        send(_coach(name="", email="sam.rivera@coach.test"))

        message = mail.outbox[0]
        assert message.body.startswith("Hi,")
        assert "<p>Hi,</p>" in message.alternatives[0][0]
        assert "Hi sam" not in message.body


class TestUnchangedEmails:
    def test_coach_request_email_keeps_default_from_and_no_reply_to(self):
        coach = _coach()

        send_coach_request_email(
            athlete=UserFactory(name="Jordan Ellis"), coach=coach, roster_url=URL
        )

        message = mail.outbox[0]
        assert message.from_email == settings.DEFAULT_FROM_EMAIL
        assert message.reply_to == []

    def test_contact_emails_unchanged(self):
        send_contact_emails("Q", "Hello", "visitor@example.com")

        owner, ack = mail.outbox
        assert owner.from_email == settings.SERVER_EMAIL
        assert owner.reply_to == ["visitor@example.com"]
        assert ack.from_email == settings.SERVER_EMAIL
        assert ack.reply_to == [settings.DEFAULT_FROM_EMAIL]

    def test_unsubscribe_headers_survive(self):
        _delivered(_coach(), unsubscribe_url="https://x.test/unsub/")
        _ended(_coach(), unsubscribe_url="https://x.test/unsub/")

        for message in mail.outbox:
            assert (
                message.extra_headers["List-Unsubscribe"] == "<https://x.test/unsub/>"
            )
            assert (
                message.extra_headers["List-Unsubscribe-Post"]
                == "List-Unsubscribe=One-Click"
            )


# -- #671: the coach's free-text name must not spoof the sender -------------


def _from_name(coach):
    name, address = parseaddr(
        mail.EmailMessage(**client_email_identity(coach)).message()["From"]
    )
    assert address == _address()
    return str(make_header(decode_header(name))), name


@pytest.mark.parametrize(
    "raw",
    [
        "support@paypal.com",
        '"><b>x',
        "Sam <evil@x.test> Rivera",
        "Sam\x00\x07\x1b[31m\r\nBcc: a@b.test Rivera",
    ],
)
def test_hostile_display_name_is_neutralised(raw):
    decoded, _ = _from_name(_coach(name=raw))
    assert decoded.endswith(" via Mastering Fitness")
    for bad in '<>@"\r\n\x00\x07\x1b':
        assert bad not in decoded


def test_display_name_is_capped_at_64_chars():
    decoded, _ = _from_name(_coach(name="A" * 200))
    assert decoded == "A" * 64 + " via Mastering Fitness"


def test_empty_after_sanitising_falls_back_to_account_name_then_generic():
    from store_project.meso.factories import CoachProfileFactory

    coach = _coach(name="Sam Rivera")
    CoachProfileFactory(user=coach, display_name='<>@"')
    assert _from_name(coach)[0] == "Sam Rivera via Mastering Fitness"

    nameless = _coach(name='<>@"', email="x@coach.test")
    assert _from_name(nameless)[0] == "Your coach via Mastering Fitness"


def test_encoded_word_name_cannot_smuggle_an_address():
    from store_project.notifications.emails import _safe_display_name

    name = _safe_display_name("=?utf-8?b?c3VwcG9ydEBwYXlwYWwuY29t?=")
    assert "=?" not in name and "?=" not in name


def test_overlapping_encoded_word_markers_cannot_reassemble():
    from store_project.notifications.emails import _safe_display_name

    name = _safe_display_name("==??utf-8?b?c3VwcG9ydEBwYXlwYWwuY29t??==")
    assert "=?" not in name and "?=" not in name
