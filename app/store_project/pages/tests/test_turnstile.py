"""Tests for Cloudflare Turnstile server-side verification.

The contact form has no other bot protection, so the behavior that matters most
here is what happens when something goes *wrong*: every failure path has to
reject the submission rather than fall through to the mail sender.
"""

from unittest import mock

import pytest
import requests
from django.test import override_settings

from store_project.pages import turnstile

PASS_PAYLOAD = {
    "success": True,
    "hostname": "testserver",
    "action": "contact",
    "error-codes": [],
}


def _fake_response(payload=None, *, json_error=None):
    """A stand-in for the ``requests`` response siteverify would return."""
    response = mock.Mock()
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = payload
    return response


def _patch_post(**kwargs):
    return mock.patch.object(turnstile.requests, "post", **kwargs)


class TestVerifyAcceptance:
    def test_accepts_a_well_formed_pass(self):
        with _patch_post(return_value=_fake_response(PASS_PAYLOAD)):
            result = turnstile.verify("a-token")

        assert result.success
        assert result.error_codes == []

    def test_accepts_a_pass_that_carries_no_action(self):
        """Cloudflare omits ``action`` for a widget that sets no data-action."""
        payload = PASS_PAYLOAD | {"action": ""}
        with _patch_post(return_value=_fake_response(payload)):
            assert turnstile.verify("a-token").success

    def test_sends_the_secret_token_and_remote_ip(self):
        with _patch_post(return_value=_fake_response(PASS_PAYLOAD)) as post:
            turnstile.verify("a-token", remote_ip="203.0.113.7")

        _, kwargs = post.call_args
        assert kwargs["data"]["secret"] == "test-turnstile-secret-key"
        assert kwargs["data"]["response"] == "a-token"
        assert kwargs["data"]["remoteip"] == "203.0.113.7"
        assert kwargs["timeout"] == turnstile.VERIFY_TIMEOUT_SECONDS

    def test_omits_remote_ip_when_it_is_unknown(self):
        with _patch_post(return_value=_fake_response(PASS_PAYLOAD)) as post:
            turnstile.verify("a-token")

        assert "remoteip" not in post.call_args.kwargs["data"]


class TestVerifyRejection:
    def test_rejects_an_empty_token_without_calling_cloudflare(self):
        with _patch_post() as post:
            result = turnstile.verify("")

        assert not result.success
        assert result.error_codes == [turnstile.MISSING_TOKEN]
        post.assert_not_called()

    def test_rejects_when_cloudflare_says_no(self):
        payload = {"success": False, "error-codes": ["invalid-input-response"]}
        with _patch_post(return_value=_fake_response(payload)):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.error_codes == ["invalid-input-response"]

    def test_rejects_a_token_solved_on_another_hostname(self):
        """A farmed token verifies as success; the hostname is what exposes it."""
        payload = PASS_PAYLOAD | {"hostname": "spammer.example"}
        with _patch_post(return_value=_fake_response(payload)):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.error_codes == [turnstile.HOSTNAME_MISMATCH]

    def test_rejects_a_token_minted_for_another_action(self):
        payload = PASS_PAYLOAD | {"action": "newsletter"}
        with _patch_post(return_value=_fake_response(payload)):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.error_codes == [turnstile.ACTION_MISMATCH]

    @override_settings(TURNSTILE_SECRET_KEY="")
    def test_rejects_everything_when_the_secret_is_missing(self):
        """Fail closed: an unconfigured deploy must not accept all comers."""
        with _patch_post() as post:
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.is_misconfigured
        post.assert_not_called()

    def test_rejects_when_cloudflare_is_unreachable(self):
        with _patch_post(side_effect=requests.ConnectionError("boom")):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.is_unavailable

    def test_rejects_when_cloudflare_returns_an_error_status(self):
        response = _fake_response(PASS_PAYLOAD)
        response.raise_for_status.side_effect = requests.HTTPError("503")
        with _patch_post(return_value=response):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.is_unavailable

    def test_rejects_when_the_reply_is_not_json(self):
        with _patch_post(return_value=_fake_response(json_error=ValueError)):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.is_unavailable

    def test_rejects_when_the_reply_is_not_an_object(self):
        with _patch_post(return_value=_fake_response(["unexpected"])):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.is_unavailable

    def test_survives_a_failure_reply_with_no_error_codes(self):
        """Regression: the old reCAPTCHA code indexed ``error-codes`` blindly."""
        with _patch_post(return_value=_fake_response({"success": False})):
            result = turnstile.verify("a-token")

        assert not result.success
        assert result.error_codes == []

    def test_survives_a_null_error_codes_list(self):
        payload = {"success": False, "error-codes": None}
        with _patch_post(return_value=_fake_response(payload)):
            assert turnstile.verify("a-token").error_codes == []


class TestHostnameCheckConfiguration:
    @override_settings(TURNSTILE_ALLOWED_HOSTNAMES=[])
    def test_skips_the_hostname_check_when_nothing_is_configured(self):
        payload = PASS_PAYLOAD | {"hostname": "anywhere.example"}
        with _patch_post(return_value=_fake_response(payload)):
            assert turnstile.verify("a-token").success

    @override_settings(
        TURNSTILE_ALLOWED_HOSTNAMES=["mastering.fitness", "www.mastering.fitness"]
    )
    def test_accepts_any_configured_hostname(self):
        payload = PASS_PAYLOAD | {"hostname": "www.mastering.fitness"}
        with _patch_post(return_value=_fake_response(payload)):
            assert turnstile.verify("a-token").success


class TestClientIp:
    def test_prefers_the_first_forwarded_hop(self, rf):
        request = rf.post("/contact/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")

        assert turnstile.client_ip(request) == "203.0.113.7"

    def test_falls_back_to_remote_addr(self, rf):
        request = rf.post("/contact/", REMOTE_ADDR="198.51.100.4")

        assert turnstile.client_ip(request) == "198.51.100.4"


@pytest.mark.parametrize(
    "codes,expected",
    [
        ([turnstile.MISSING_SECRET], True),
        (["invalid-input-secret"], True),
        (["invalid-input-response"], False),
        ([], False),
    ],
)
def test_is_misconfigured_flags_only_our_own_errors(codes, expected):
    assert turnstile.TurnstileResult(False, codes).is_misconfigured is expected
