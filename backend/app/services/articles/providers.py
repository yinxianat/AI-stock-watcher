"""Which AI writes the stories: Anthropic (Claude) or Google (Gemini).

Every stage except Claude's web-search research only needs "send a system
prompt and a user prompt, get JSON back in this shape", so both providers
implement just that (`generate`). Pick with AI_PROVIDER; AI_API_KEY is the
key for that provider.

Gemini is called through its REST API with httpx (already a dependency)
rather than another SDK. Its free tier costs nothing but has low rate limits,
and Google may use free-tier prompts to improve its products — we only ever
send public news and stock moves, never anything about users.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Protocol

import httpx

from app.core.settings import get_settings
from app.services.articles.schemas import Usage


class StoryAIError(Exception):
    """The AI didn't give a usable answer (declined, cut off, no answer)."""


class AIServiceError(Exception):
    """An HTTP error from an AI or news service. `service` and `status_code`
    let the pipeline tell key/quota problems (admin is emailed) apart from
    passing outages."""

    def __init__(self, service: str, status_code: int | None, message: str):
        super().__init__(f"{service} HTTP {status_code}: {message}")
        self.service = service
        self.status_code = status_code


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic's JSON schema, made strict for structured output: every
    object closed to extra keys and every field required."""
    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                fix(value)
        elif isinstance(node, list):
            for value in node:
                fix(value)
        return node
    return fix(copy.deepcopy(schema))


class JSONGenerator(Protocol):
    name: str

    def generate(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[str, Usage]:
        """Return (JSON text matching `schema`, token usage)."""
        ...


class AnthropicGenerator:
    name = "Anthropic"

    def generate(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[str, Usage]:
        from app.services.articles import claude

        response = claude.create(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        )
        claude.check_stop(response, "answer")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise StoryAIError("no JSON in the answer")
        return text, claude.usage_of(response)


_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GEMINI_TIMEOUT = httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=10.0)
_RETRY_STATUSES = {429, 500, 503}
_RETRY_WAITS = (2.0, 6.0)  # seconds before the 2nd and 3rd attempt


def _gemini_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        return f"{err.get('status', '')} {err.get('message', '')}".strip()[:300]
    except Exception:  # noqa: BLE001
        return r.text[:300]


class GeminiGenerator:
    name = "Gemini"

    def generate(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[str, Usage]:
        settings = get_settings()
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        url = _GEMINI_URL.format(model=settings.model_for("gemini"))
        # Key in a header, not the URL, so it never shows up in logs.
        headers = {"x-goog-api-key": settings.key_for("gemini")}
        r: httpx.Response | None = None
        for attempt in range(len(_RETRY_WAITS) + 1):
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=_GEMINI_TIMEOUT)
            except httpx.HTTPError as e:
                if attempt == len(_RETRY_WAITS):
                    raise AIServiceError("Gemini", None, str(e)) from e
                time.sleep(_RETRY_WAITS[attempt])
                continue
            if r.status_code in _RETRY_STATUSES and attempt < len(_RETRY_WAITS):
                time.sleep(_RETRY_WAITS[attempt])
                continue
            break
        assert r is not None
        if r.status_code != 200:
            raise AIServiceError("Gemini", r.status_code, _gemini_error(r))

        data = r.json()
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise StoryAIError(f"Gemini declined the request ({blocked})")
        candidate = (data.get("candidates") or [None])[0]
        if candidate is None:
            raise StoryAIError("Gemini returned no answer")
        finish = candidate.get("finishReason")
        if finish == "MAX_TOKENS":
            raise StoryAIError("Gemini's answer was cut off")
        if finish not in (None, "STOP"):
            raise StoryAIError(f"Gemini stopped early ({finish})")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        meta = data.get("usageMetadata") or {}
        usage = Usage(
            input_tokens=meta.get("promptTokenCount", 0) or 0,
            output_tokens=(meta.get("candidatesTokenCount", 0) or 0)
            + (meta.get("thoughtsTokenCount", 0) or 0),
        )
        return text, usage


def get_generator(provider: str | None = None) -> JSONGenerator:
    provider = provider or get_settings().ai_provider
    return AnthropicGenerator() if provider == "anthropic" else GeminiGenerator()
