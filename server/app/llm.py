import logging
from typing import Optional, Protocol, Tuple
import httpx
from app.config import RETIRED_MODEL_REPLACEMENTS, settings
from app.scrub import scrub_text

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        ...


class AnthropicClient:
    """Anthropic Claude LLM provider implementation."""

    # Server-side refusal fallback: a policy decline is retried on Anthropic's recommended model
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout: float = 45.0,
        http_client: Optional[object] = None,
    ):
        import anthropic
        key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model or settings.LLM_MODEL_FAST
        self.effort = effort or settings.LLM_EFFORT_FAST
        # Pass the key only when it is set: the SDK uses a passed credential as the only one
        self.client = anthropic.AsyncAnthropic(api_key=key or None, timeout=timeout, http_client=http_client)
        self.last_usage: Optional[Tuple[int, int]] = None

    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        clean_system = scrub_text(system)
        clean_prompt = scrub_text(prompt)

        # Thinking is always on for current models and counts toward max_tokens, so callers pass room for it
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=clean_system,
            messages=[{"role": "user", "content": clean_prompt}],
            output_config={"effort": self.effort},
            betas=[self.FALLBACK_BETA],
            fallbacks="default",
        )
        self.last_usage = (response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            logger.warning("Claude declined the request (category: %s); no AI text this time", category)
            return ""
        if response.stop_reason == "max_tokens":
            logger.warning("Claude reached max_tokens=%d; the answer may be cut off", max_tokens)
        # The content can hold thinking and fallback blocks before the text
        return "".join(block.text for block in response.content if block.type == "text")


class OllamaClient:
    """Local Ollama LLM provider implementation using httpx."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self.last_usage: Optional[Tuple[int, int]] = None

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
            self.last_usage = (int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0))
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


def get_llm_client(deep: bool = False) -> LLMClient:
    """Factory to retrieve configured LLM provider instance; `deep` selects the stronger model."""
    provider = settings.LLM_PROVIDER.lower().strip()
    if settings.DRY_RUN_LLM or provider == "fake":
        return FakeClient()
    elif provider == "ollama":
        return OllamaClient()
    elif provider == "anthropic":
        if deep:
            return AnthropicClient(model=settings.LLM_MODEL_DEEP, effort=settings.LLM_EFFORT_DEEP, timeout=120.0)
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
            "LLM Provider: anthropic (cloud API active, scrubbed diffs and stack traces are sent to the Anthropic API; "
            "models %s and %s for Dig deeper)",
            settings.LLM_MODEL_FAST,
            settings.LLM_MODEL_DEEP,
        )
        for name, old, new in sorted(set(RETIRED_MODEL_REPLACEMENTS)):
            logger.warning(
                "%s=%s names a model Anthropic has retired; using %s instead. Update it in .env "
                "(LLM_MODEL_FAST / LLM_MODEL_DEEP).",
                name,
                old,
                new,
            )
    else:
        logger.info("LLM Provider: %s", provider)
