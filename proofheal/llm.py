"""Meta Model API client (OpenAI-compatible) with a startup capability check.

ProofHeal requires two provider features and never emulates them:
  * structured output via `response_format: {type: json_schema, strict: true}`
  * the `reasoning_effort` parameter (verification runs at "high")
Tool calling is also probed because the healing agent depends on it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from openai import APIError, OpenAI

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"


class SetupError(RuntimeError):
    """Missing configuration (e.g. no API key)."""


class IncompatibleProvider(RuntimeError):
    """The configured provider/model lacks a capability ProofHeal requires."""


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        key = os.environ.get("PROOFHEAL_API_KEY") or os.environ.get("MODEL_API_KEY")
        if not key:
            raise SetupError(
                "No API key found. Get a key from the Meta Model API dashboard and run:\n"
                "    export MODEL_API_KEY=<your key>\n"
                "(PROOFHEAL_API_KEY takes precedence if set.)"
            )
        return cls(
            base_url=os.environ.get("PROOFHEAL_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("PROOFHEAL_MODEL", DEFAULT_MODEL),
            api_key=key,
        )


@dataclass
class CallLog:
    purpose: str
    reasoning_effort: str | None
    seconds: float
    usage: dict[str, Any] | None


class LLM:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=180, max_retries=2)
        self.calls: list[CallLog] = []

    # -- low level -----------------------------------------------------------------
    def chat(self, purpose: str, messages: list[dict], *, reasoning_effort: str | None = None, **kw: Any):
        args: dict[str, Any] = {"model": self.config.model, "messages": messages, **kw}
        if reasoning_effort:
            args["reasoning_effort"] = reasoning_effort
        t0 = time.time()
        resp = self.client.chat.completions.create(**args)
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
        self.calls.append(CallLog(purpose, reasoning_effort, round(time.time() - t0, 2), usage))
        return resp

    def structured(
        self, purpose: str, messages: list[dict], schema_name: str, schema: dict, *, reasoning_effort: str, **kw: Any
    ) -> dict[str, Any]:
        """One call with a strict JSON-schema response format; output is validated, never repaired."""
        resp = self.chat(
            purpose,
            messages,
            reasoning_effort=reasoning_effort,
            response_format={"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
            **kw,
        )
        content = resp.choices[0].message.content or ""
        try:
            data = json.loads(content)
            jsonschema.validate(data, schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            raise IncompatibleProvider(
                f"{purpose}: model output did not satisfy the JSON schema {schema_name!r}: {exc}"
            ) from exc
        return data

    # -- capability check ----------------------------------------------------------
    def capability_check(self) -> dict[str, Any]:
        """Probe structured output + reasoning_effort, then tool calling. Raise on anything missing."""
        where = f"{self.config.model} at {self.config.base_url}"
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
            "required": ["ok", "word"],
            "additionalProperties": False,
        }
        msgs = [{"role": "user", "content": "Capability probe. Reply with ok=true and word='proof'."}]
        try:
            out = self.structured("capability: structured+reasoning", msgs, "probe", schema, reasoning_effort="high")
        except APIError as exc:
            raise IncompatibleProvider(
                f"{where} rejected a request using response_format=json_schema (strict) with reasoning_effort='high'. "
                f"Both are required; ProofHeal will not emulate them. Provider said: {_err(exc)}"
            ) from exc
        if out.get("ok") is not True:
            raise IncompatibleProvider(f"{where}: structured-output probe returned unexpected content {out!r}")

        tool = {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Health-check tool.",
                "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
            },
        }
        try:
            resp = self.chat(
                "capability: tool calling",
                [{"role": "user", "content": "Call the ping tool with note='hi'."}],
                tools=[tool],
                tool_choice="required",
                reasoning_effort="low",
            )
        except APIError as exc:
            raise IncompatibleProvider(f"{where} rejected a tool-calling request: {_err(exc)}") from exc
        if not resp.choices[0].message.tool_calls:
            raise IncompatibleProvider(f"{where} did not return a tool call when one was required.")
        return {"model": self.config.model, "base_url": self.config.base_url, "structured_output": True,
                "reasoning_effort": "high", "tool_calling": True}


def _err(exc: APIError) -> str:
    msg = getattr(exc, "message", None) or str(exc)
    return msg[:400]
