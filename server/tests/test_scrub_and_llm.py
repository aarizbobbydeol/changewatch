import pytest
from app.llm import FakeClient, get_llm_client, log_llm_privacy_notice
from app.scrub import scrub_text


def test_scrub_secrets():
    raw_text = (
        "Here is my config:\n"
        "api_key: 'super_secret_12345'\n"
        "password = 'admin_password_99'\n"
        "AWS_KEY=AKIA1234567890ABCDEF\n"
        "GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890\n"
        "SK_TOKEN=sk-abcdefghijklmnopqrstuvwxyz123456\n"
    )
    scrubbed = scrub_text(raw_text)

    # Asserts that sensitive values are removed
    assert "super_secret_12345" not in scrubbed
    assert "admin_password_99" not in scrubbed
    assert "AKIA1234567890ABCDEF" not in scrubbed
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in scrubbed
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in scrubbed

    # Asserts that redaction markers are present
    assert "[REDACTED_AWS_KEY]" in scrubbed
    assert "[REDACTED_GITHUB_TOKEN]" in scrubbed
    assert "[REDACTED_SECRET_KEY]" in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed


@pytest.mark.asyncio
async def test_fake_llm_client():
    client = FakeClient()
    answer = await client.complete(
        system="You are an SRE.",
        prompt="Analyze this error with token ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        max_tokens=300,
    )
    assert "Likely cause" in answer
    assert "Fix:" in answer


def test_llm_factory_and_privacy_notice(caplog):
    import logging
    caplog.set_level(logging.INFO)

    client = get_llm_client()
    assert client is not None

    log_llm_privacy_notice()
    assert any("LLM Provider" in record.message for record in caplog.records)
