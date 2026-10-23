"""Provider-neutral LLM interface for Member 1 (RAG / LangGraph).

What lives here
    * the result / error / health types every caller uses,
    * ``MockLLM``  - deterministic, offline, never touches the network,
    * ``get_llm()`` - picks the implementation from environment variables.

The real implementation is in ``llm/openai_client.py`` (OpenAI Responses API).
It is imported lazily, so ``MOCK_LLM=true`` works without the ``openai``
package installed and without any API key.

Rules this module enforces
    * ``MOCK_LLM`` is an explicit switch. A failed real call is NEVER turned
      into a mock answer; callers get a controlled ``LLMError`` instead.
    * ``str(error)`` never contains an API key or prompt text.
    * The LLM only writes wording. It is not a source of facts, citations or
      calculations (official brief R1, R5): callers pass evidence in and take
      citations from the retrieved chunks, never from model output.
"""
from __future__ import annotations

import logging
import os
import re
import time
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Type, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("llm")

T = TypeVar("T", bound=BaseModel)

MOCK_PROVIDER = "mock"
OPENAI_PROVIDER = "openai"
MOCK_MODEL = "MOCK"
MOCK_TEXT = "[MOCK LLM] No real model was called."

_TRUTHY = {"1", "true", "yes", "on"}
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),   # OpenAI-style keys (incl. sk-proj-...)
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
)
_DETAIL_LIMIT = 300
# `X | None` has origin types.UnionType on Python 3.10+; typing.Union covers Optional[X] everywhere.
_UNION_ORIGINS = (typing.Union,) + ((types.UnionType,) if hasattr(types, "UnionType") else ())


def scrub(text: str) -> str:
    """Redact anything that looks like an API key / bearer token, then truncate."""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out if len(out) <= _DETAIL_LIMIT else out[:_DETAIL_LIMIT] + "..."


# --------------------------------------------------------------------------- types
@dataclass(frozen=True)
class LLMUsage:
    """Token counts as reported by the provider (0 when unknown / mock)."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class LLMResult:
    """What a successful ``generate`` returns.

    ``llm_calls`` counts provider requests (2 when the one allowed retry was used).
    ``usage`` / ``llm_calls`` / ``model`` / ``latency_ms`` feed the audit record (R10).
    """

    text: str
    parsed: Optional[BaseModel]
    provider: str
    model: str
    usage: LLMUsage
    latency_ms: int
    llm_calls: int
    response_id: Optional[str] = None


@dataclass(frozen=True)
class LLMHealth:
    """Result of an ACTUAL check. ``status`` is "ok" only if a real request succeeded."""

    status: Literal["ok", "down", "mock"]
    provider: str
    model: str
    detail: str = ""


class LLMError(Exception):
    """Base class for every controlled LLM failure.

    ``str(exc)`` is always safe to log or show. ``detail`` is a scrubbed, truncated
    provider message for developers. ``usage`` / ``llm_calls`` describe what was spent
    before the failure so the audit record can still be written.
    """

    default_kind = "error"

    def __init__(
        self,
        message: str,
        *,
        kind: Optional[str] = None,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind or self.default_kind
        self.status_code = status_code
        self.request_id = request_id
        self.detail = scrub(detail) if detail else ""
        self.usage: Optional[LLMUsage] = None
        self.llm_calls: int = 0


class LLMConfigError(LLMError):
    """Missing / invalid configuration (no key, bad env value, package not installed)."""

    default_kind = "config"


class LLMTimeoutError(LLMError):
    default_kind = "timeout"


class LLMAPIError(LLMError):
    """Provider returned an error. kind: auth | rate_limit | bad_request | not_found |
    server | connection | response_failed | other."""

    default_kind = "other"


class LLMOutputError(LLMError):
    """The model answered but not usably. kind: invalid | empty | refusal | incomplete."""

    default_kind = "invalid"


@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    def generate(
        self, *, system: str, user: str, response_model: Optional[Type[T]] = None
    ) -> LLMResult: ...

    def health(self) -> LLMHealth: ...


# --------------------------------------------------------------------------- mock
def _example_value(annotation: Any, field_name: str) -> Any:
    """A valid placeholder value for a field type (used only by MockLLM)."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in _UNION_ORIGINS:
        if type(None) in args:
            return None
        return _example_value(args[0], field_name)
    if origin is Literal:
        return args[0]
    if origin in (list, set, frozenset, tuple):
        return []
    if origin is dict or annotation is dict:
        return {}
    if annotation is bool:        # before int: bool is a subclass of int
        return False
    if annotation is str:
        return "[MOCK]"
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _example_instance(annotation)
    raise LLMConfigError(
        f"MockLLM cannot build an example for field '{field_name}'; pass a responder to MockLLM."
    )


def _example_instance(model: Type[T]) -> T:
    values = {
        name: _example_value(field.annotation, name)
        for name, field in model.model_fields.items()
        if field.is_required()
    }
    return model.model_validate(values)


class MockLLM:
    """Deterministic offline LLM for unit tests and demos without an API key.

    * Never imports the ``openai`` package and never opens a network connection.
    * Reports ``provider="mock"`` and ``model="MOCK"`` so audit records show it plainly.
    * ``responder(system, user, response_model)`` may return a str, a dict or a model
      instance to script an answer; otherwise a labelled placeholder is returned.
    * ``llm_calls`` is 1 per ``generate`` so audit plumbing can be tested; tokens are 0.
    * ``self.calls`` records every request (tests assert on it).
    """

    provider = MOCK_PROVIDER
    model = MOCK_MODEL

    def __init__(self, responder: Optional[Callable[[str, str, Optional[type]], Any]] = None) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return "MockLLM()"

    def generate(
        self, *, system: str, user: str, response_model: Optional[Type[T]] = None
    ) -> LLMResult:
        started = time.perf_counter()
        self.calls.append({"system": system, "user": user, "response_model": response_model})
        raw = self._responder(system, user, response_model) if self._responder else None

        parsed: Optional[BaseModel] = None
        if response_model is None:
            text = raw if isinstance(raw, str) else MOCK_TEXT
        else:
            try:
                if raw is None:
                    parsed = _example_instance(response_model)
                elif isinstance(raw, response_model):
                    parsed = raw
                elif isinstance(raw, str):
                    parsed = response_model.model_validate_json(raw)
                else:
                    parsed = response_model.model_validate(raw)
            except ValidationError as exc:
                raise LLMOutputError(
                    "Mock responder output does not match response_model.",
                    kind="invalid",
                    detail=_validation_summary(exc),
                ) from exc
            text = parsed.model_dump_json()

        return LLMResult(
            text=text,
            parsed=parsed,
            provider=self.provider,
            model=self.model,
            usage=LLMUsage(),
            latency_ms=int((time.perf_counter() - started) * 1000),
            llm_calls=1,
        )

    def health(self) -> LLMHealth:
        return LLMHealth("mock", self.provider, self.model, "MOCK_LLM mode: no real model is called.")


def _validation_summary(exc: ValidationError) -> str:
    """Field paths + error types only. Never the input values (they could echo prompt text)."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('type', 'invalid')}")
    return "; ".join(parts[:8])


# --------------------------------------------------------------------------- factory
def is_mock_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("MOCK_LLM", "")).strip().lower() in _TRUTHY


def get_llm(env: Optional[Mapping[str, str]] = None) -> LLMClient:
    """MOCK_LLM=true -> MockLLM. Otherwise the OpenAI client, configured from env.

    Raises LLMConfigError when the real client cannot be configured. It never falls
    back to the mock on its own.
    """
    env = os.environ if env is None else env
    if is_mock_enabled(env):
        return MockLLM()
    from llm.openai_client import OpenAIClient  # lazy: keeps `openai` optional for mock use

    return OpenAIClient.from_env(env)
