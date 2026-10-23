import logging
from typing import Optional, Protocol
import httpx
from app.config import settings
from app.scrub import scrub_text

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        ...


class AnthropicClient:
    """Anthropic Claude LLM provider implementation."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        import anthropic
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model or settings.LLM_MODEL
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=30.0)

    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        clean_system = scrub_text(system)
        clean_prompt = scrub_text(prompt)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=clean_system,
            messages=[{"role": "user", "content": clean_prompt}],
        )
        if response.content and len(response.content) > 0:
            return response.content[0].text
        return ""


class OllamaClient:
    """Local Ollama LLM provider implementation using httpx."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL

        import urllib.parse
        parsed = urllib.parse.urlparse(self.base_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and hostname not in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
            logger.warning(
                "ChangeWatch security warning: Ollama configured over non-local unencrypted HTTP (%s). "
                "Consider using HTTPS or an encrypted VPN tunnel to protect model queries.",
                self.base_url,
            )


    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        clean_system = scrub_text(system)
        clean_prompt = scrub_text(prompt)

        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": clean_system},
                {"role": "user", "content": clean_prompt},
            ],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")


class FakeClient:
    """Deterministic canned answer provider for tests and dry-run mode."""

    def __init__(self, canned_answer: Optional[str] = None):
        self.canned_answer = (
            canned_answer
            or "Likely cause: Unhandled KeyError in user_service.py:fetch_profile. "
            "Fix: Verify key presence or use dict.get() with fallback."
        )

    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        # Secret scrubbing must still run before every provider call
        _ = scrub_text(system)
        _ = scrub_text(prompt)
        return self.canned_answer


def get_llm_client() -> LLMClient:
    """Factory to retrieve configured LLM provider instance."""
    provider = settings.LLM_PROVIDER.lower().strip()
    if settings.DRY_RUN_LLM or provider == "fake":
        return FakeClient()
    elif provider == "ollama":
        return OllamaClient()
    elif provider == "anthropic":
        return AnthropicClient()
    else:
        logger.warning(
            "Unknown LLM_PROVIDER '%s', falling back to FakeClient", provider
        )
        return FakeClient()


def log_llm_privacy_notice():
    """
    Log privacy disclosure on startup as required by Addendum Section B.
    """
    provider = settings.LLM_PROVIDER.lower().strip()
    if settings.DRY_RUN_LLM or provider == "fake":
        logger.info(
            "LLM Provider: fake (dry-run mode active, returning deterministic test answers)"
        )
    elif provider == "ollama":
        logger.info(
            "LLM Provider: ollama (local server active, data stays on this network)"
        )
    elif provider == "anthropic":
        logger.info(
            "LLM Provider: anthropic (cloud API active, scrubbed diffs and stack traces are sent to the Anthropic API)"
        )
    else:
        logger.info("LLM Provider: %s", provider)
