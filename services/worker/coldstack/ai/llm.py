"""BYOK LLM client.

One interface, three backends (Anthropic, OpenAI, a local Ollama), plus a scripted fake
so every prompt-shaped behaviour in this package is tested without a key or a network
call. Model choice is the user's - we never hardcode a vendor.

Two rules that hold across every call site in this package:

* **JSON in, JSON out, validated.** Free text from a model is never fed straight into
  a query, a schema, or an email. It is parsed, checked against what the caller can
  actually represent, and rejected on mismatch.
* **A failed LLM call degrades, never raises into the send path.** Copy generation
  failing should leave the user with a blank draft, not a half-sent campaign.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx


@dataclass(slots=True)
class LlmResult:
    text: str
    ok: bool = True
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def json(self) -> Any:
        """Models wrap JSON in prose and fences no matter how firmly you ask them not
        to, so extract the first balanced object rather than trusting the whole body."""
        if not self.ok:
            return None
        body = self.text.strip()
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.M).strip()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
        start = body.find("{")
        if start == -1:
            return None
        depth = 0
        for i, ch in enumerate(body[start:], start):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                try:
                    return json.loads(body[start:i + 1])
                except json.JSONDecodeError:
                    return None
        return None


@runtime_checkable
class Llm(Protocol):
    name: str
    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                       temperature: float = 0.3) -> LlmResult: ...


# Rough list prices per million tokens, for the cost ledger. Refined from usage.
_PRICES = {"anthropic": (3.0, 15.0), "openai": (2.5, 10.0), "ollama": (0.0, 0.0)}


@dataclass
class AnthropicLlm:
    api_key: str
    model: str = "claude-sonnet-4-5"
    name: str = "anthropic"

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                       temperature: float = 0.3) -> LlmResult:
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                                 headers={"x-api-key": self.api_key,
                                          "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"},
                                 json=body)
        except httpx.HTTPError as exc:
            return LlmResult("", ok=False, error=str(exc))
        if r.status_code >= 400:
            return LlmResult("", ok=False, error=f"anthropic {r.status_code}: {r.text[:300]}")
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []))
        usage = d.get("usage") or {}
        return _priced(self.name, text, usage.get("input_tokens", 0),
                       usage.get("output_tokens", 0))


@dataclass
class OpenAiLlm:
    api_key: str
    model: str = "gpt-4.1"
    name: str = "openai"

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                       temperature: float = 0.3) -> LlmResult:
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post("https://api.openai.com/v1/chat/completions",
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 json={"model": self.model, "messages": messages,
                                       "max_tokens": max_tokens,
                                       "temperature": temperature})
        except httpx.HTTPError as exc:
            return LlmResult("", ok=False, error=str(exc))
        if r.status_code >= 400:
            return LlmResult("", ok=False, error=f"openai {r.status_code}: {r.text[:300]}")
        d = r.json()
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        u = d.get("usage") or {}
        return _priced(self.name, text, u.get("prompt_tokens", 0),
                       u.get("completion_tokens", 0))


@dataclass
class OllamaLlm:
    base_url: str = "http://localhost:11434"
    model: str = "llama3.1"
    name: str = "ollama"

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                       temperature: float = 0.3) -> LlmResult:
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(f"{self.base_url.rstrip('/')}/api/generate",
                                 json={"model": self.model, "prompt": prompt,
                                       "system": system, "stream": False,
                                       "options": {"temperature": temperature,
                                                   "num_predict": max_tokens}})
        except httpx.HTTPError as exc:
            return LlmResult("", ok=False, error=str(exc))
        if r.status_code >= 400:
            return LlmResult("", ok=False, error=f"ollama {r.status_code}")
        return LlmResult((r.json() or {}).get("response", ""))


@dataclass
class FakeLlm:
    """Scripted responses, so prompt-handling logic is testable and free."""
    responses: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    ok: bool = True
    name: str = "fake"

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                       temperature: float = 0.3) -> LlmResult:
        self.prompts.append(prompt)
        if not self.ok:
            return LlmResult("", ok=False, error="fake failure")
        text = self.responses.pop(0) if self.responses else "{}"
        return LlmResult(text)


def _priced(vendor: str, text: str, inp: int, out: int) -> LlmResult:
    pin, pout = _PRICES.get(vendor, (0.0, 0.0))
    return LlmResult(text, input_tokens=inp, output_tokens=out,
                     cost_usd=(inp * pin + out * pout) / 1_000_000)


def from_env() -> Llm | None:
    if os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicLlm(os.environ["ANTHROPIC_API_KEY"])
    if os.getenv("OPENAI_API_KEY"):
        return OpenAiLlm(os.environ["OPENAI_API_KEY"])
    if os.getenv("OLLAMA_URL"):
        return OllamaLlm(os.environ["OLLAMA_URL"])
    return None
