"""Secrets the scrubbers used to miss (server, SDK and logs), and URLs and emails in logs."""
import logging
import time

import pytest

from app.logging_filter import LogRedactionFilter, redact_delivery_error
from app.scrub import scrub_text
from changewatch import _scrub as sdk_scrub

LEAKS = [
    # (text, secret that must disappear, text that must stay)
    ("401 for url: https://oauth2.example.com/token?client_secret=s3cr3tvalue&refresh_token=1//0gabc",
     ["s3cr3tvalue", "1//0gabc"], "oauth2.example.com"),
    ("bad config {'password': 'hunter2', 'user': 'bob'}", ["hunter2"], "'user': 'bob'"),
    ('{"api_key": "abc123secretvalue", "region": "eu"}', ["abc123secretvalue"], '"region": "eu"'),
    ('+DB_PASSWORD = "hunter2hunter2"', ["hunter2hunter2"], "DB_PASSWORD"),
    ("STRIPE_SECRET_KEY=sk_live_51Habcdefghijk AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIabcdef",
     ["sk_live_51Habcdefghijk", "wJalrXUtnFEMIabcdef"], "STRIPE_SECRET_KEY"),
    ("input_value={'password': 'my long passphrase'}", ["my long passphrase"], "input_value"),
    ("token github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", ["github_pat_11ABCDEFG0123456789"], "token"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1.abc.def-ghi_jklmnop", ["eyJhbGciOiJIUzI1"], "Authorization"),
    ("slack xoxb-1234567890-abcdefghij posted", ["xoxb-1234567890-abcdefghij"], "posted"),
]


@pytest.mark.parametrize("text,secrets,kept", LEAKS)
def test_server_scrubber_removes_the_secret(text, secrets, kept):
    out = scrub_text(text)
    for secret in secrets:
        assert secret not in out, out
    assert kept in out


@pytest.mark.parametrize("text,secrets,kept", LEAKS)
def test_sdk_scrubber_removes_the_secret(text, secrets, kept):
    out = sdk_scrub(text)
    for secret in secrets:
        assert secret not in out, out
    assert kept in out


def test_quotes_around_the_value_are_kept():
    assert scrub_text('api_key="abcdefgh12345"') == 'api_key="[REDACTED_SECRET]"'
    assert scrub_text("secret=abcdefgh12345") == "secret=[REDACTED_SECRET]"


def test_ordinary_words_are_left_alone():
    text = "token_count=42 tokens used, author=dev, password reset email sent"
    assert scrub_text(text) == text


@pytest.mark.parametrize("scrub", [scrub_text, sdk_scrub])
def test_scrubbers_stay_linear_on_long_runs(scrub):
    for blob in ("a-" * 20000, "a_" * 20000, "x" * 40000, "password" * 5000):
        start = time.perf_counter()
        scrub(blob)
        assert time.perf_counter() - start < 0.5


def _logged(msg, *args) -> str:
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)
    LogRedactionFilter().filter(record)
    return record.msg


def test_log_lines_never_carry_credential_urls():
    teams = ("https://prod-12.westus.logic.azure.com:443/workflows/abc123/triggers/manual/paths/invoke"
             "?api-version=2016-06-01&sig=SECRETSIG")
    zapier = "https://hooks.zapier.com/hooks/catch/123/abcSECRET/"
    out = _logged('HTTP Request: POST %s "HTTP/1.1 200 OK"', teams) + _logged("posting to %s", zapier)
    assert "SECRETSIG" not in out and "abcSECRET" not in out
    assert "prod-12.westus.logic.azure.com" in out  # the host is still useful


def test_httpx_request_logging_is_off():
    from app.main import setup_structured_logging

    setup_structured_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_delivery_errors_do_not_carry_recipient_emails():
    err = "{'oncall@acme.com': (550, b'5.1.1 User unknown')}"
    out = redact_delivery_error(err)
    assert "oncall@acme.com" not in out and "User unknown" in out
    assert "oncall@acme.com" not in _logged("Outbox delivery failed: %s", err)


@pytest.mark.parametrize("scrub", [scrub_text, sdk_scrub])
def test_scrubbing_twice_changes_nothing(scrub):
    for text, _, _ in LEAKS:
        once = scrub(text)
        assert scrub(once) == once
