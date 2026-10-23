"""OpenAI LLM client (Responses API) with a safe offline mock mode.

Environment variables
---------------------
OPENAI_API_KEY          required unless MOCK_LLM=true. Read from the environment only;
                        never printed, logged, stored in repr() or put in error messages.
OPENAI_MODEL            model id (default: gpt-6-luna). Any Responses-API model that
                        supports Structured Outputs works.
OPENAI_TIMEOUT_SECONDS  per-request timeout (default 30).
OPENAI_MAX_RETRIES      SDK-level retries for transient network/5xx/429 errors (default 1).
                        This is separate from the ONE retry allowed for invalid output.
MOCK_LLM                "true"/"1"/"yes" -> no network, no key needed, deterministic output.

Two entry points
----------------
generate_text(system, user)                  -> LLMResult
generate_structured(system, user, Schema)    -> StructuredResult  (Pydantic-validated)

Invalid structured output (bad JSON, schema violation, refusal, truncation) is retried
at most ONCE with a corrective instruction; a second failure raises LLMOutputError.
Timeouts and API errors are never retried by this layer (the SDK handles transient
retries) and surface as LLMTimeoutError / LLMAPIError with sanitised messages.

Prompts and answers are never logged here; only model, latency and token counts are.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("llm")

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_SDK_MAX_RETRIES = 1
MOCK_MODEL_NAME = "mock-llm"
_TRUE = {"1", "true", "yes", "on"}

T = TypeVar("T", bound=BaseModel)

_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-\*\.]{4,}")
CORRECTION_MESSAGE = (
    "Your previous reply was not valid for the required schema. Reply again with ONLY a "
    "response that matches the schema exactly. Do not add commentary."
)


# --------------------------------------------------------------------------- errors
class LLMError(Exception):
    """Base class. ``code`` is stable and machine-readable; messages never contain secrets."""
    code = "llm_error"

    def __init__(self, message: str):
        super().__init__(_redact(message))


class LLMConfigError(LLMError):
    code = "llm_config_error"


class LLMTimeoutError(LLMError):
    code = "llm_timeout"


class LLMAPIError(LLMError):
    code = "llm_api_error"

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class LLMOutputError(LLMError):
    code = "llm_invalid_output"


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted-key]", str(text))


# --------------------------------------------------------------------------- config
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise LLMConfigError(f"{name} must be a number") from None
    if value <= 0:
        raise LLMConfigError(f"{name} must be greater than zero")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise LLMConfigError(f"{name} must be a whole number") from None
    if value < 0:
        raise LLMConfigError(f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class LLMConfig:
    api_key: Optional[str] = field(default=None, repr=False)  # never shown in repr()
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sdk_max_retries: int = DEFAULT_SDK_MAX_RETRIES
    mock: bool = False

    @classmethod
    def from_env(cls) -> "LLMConfig":
        mock = os.environ.get("MOCK_LLM", "").strip().lower() in _TRUE
        key = os.environ.get("OPENAI_API_KEY", "").strip() or None
        model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
        return cls(api_key=key, model=model,
                   timeout_seconds=_env_float("OPENAI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
                   sdk_max_retries=_env_int("OPENAI_MAX_RETRIES", DEFAULT_SDK_MAX_RETRIES),
                   mock=mock)

    def check(self) -> None:
        if not self.mock and not self.api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set (set it, or use MOCK_LLM=true for offline mode)")

    @property
    def model_name(self) -> str:
        return MOCK_MODEL_NAME if self.mock else self.model


# --------------------------------------------------------------------------- results
@dataclass
class LLMResult:
    text: str
    model: str
    llm_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    mock: bool = False


@dataclass
class StructuredResult:
    parsed: Any  # instance of the requested Pydantic schema
    model: str
    llm_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    retried: bool = False
    mock: bool = False


# --------------------------------------------------------------------------- mock
def _mock_value(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    """Smallest valid JSON value for a JSON schema (enough for offline tests)."""
    if "$ref" in schema:
        return _mock_value(defs[schema["$ref"].split("/")[-1]], defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            non_null = [s for s in schema[key] if s.get("type") != "null"]
            return _mock_value(non_null[0], defs) if non_null else None
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {name: _mock_value(sub, defs) for name, sub in props.items() if name in schema.get("required", [])}
    if kind == "array":
        return []
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return False
    if kind == "string":
        return "mock"
    return None


def mock_instance(schema_cls: Type[T]) -> T:
    schema = schema_cls.model_json_schema()
    return schema_cls.model_validate(_mock_value(schema, schema.get("$defs", {})))


# --------------------------------------------------------------------------- client
class LLMClient:
    """Thin wrapper over the OpenAI Responses API. Safe to construct in mock mode with no key."""

    def __init__(self, config: Optional[LLMConfig] = None, *, sdk_client: Any = None):
        self.config = config or LLMConfig.from_env()
        self.config.check()
        self._sdk = sdk_client  # injectable for tests; lazily built otherwise
        self.mock_text = "[MOCK] This is a mock LLM response."

    @property
    def mock(self) -> bool:
        return self.config.mock

    @property
    def model_name(self) -> str:
        return self.config.model_name

    # ---- SDK plumbing -----------------------------------------------------------
    def _sdk_client(self) -> Any:
        if self._sdk is None:
            from openai import OpenAI  # imported lazily: mock mode never touches the SDK
            self._sdk = OpenAI(api_key=self.config.api_key, timeout=self.config.timeout_seconds,
                               max_retries=self.config.sdk_max_retries)
        return self._sdk

    @staticmethod
    def _usage(response: Any) -> tuple[int, int, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0, 0
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or (inp + out))
        return inp, out, total

    def _translate(self, exc: Exception) -> LLMError:
        """Map SDK exceptions to our sanitised errors. Never echoes the SDK message verbatim
        (it can contain a partial API key)."""
        import openai
        if isinstance(exc, openai.APITimeoutError):
            return LLMTimeoutError(f"The LLM request timed out after {self.config.timeout_seconds:g}s")
        if isinstance(exc, openai.AuthenticationError):
            return LLMAPIError("The LLM provider rejected the API key (authentication failed)", 401)
        if isinstance(exc, openai.PermissionDeniedError):
            return LLMAPIError("The LLM provider denied access to this model or project", 403)
        if isinstance(exc, openai.RateLimitError):
            return LLMAPIError("The LLM provider rate limit or quota was reached", 429)
        if isinstance(exc, openai.NotFoundError):
            return LLMAPIError("The configured OPENAI_MODEL was not found for this account", 404)
        if isinstance(exc, openai.APIStatusError):
            return LLMAPIError(f"The LLM provider returned HTTP {exc.status_code}", exc.status_code)
        if isinstance(exc, openai.APIConnectionError):
            return LLMAPIError("Could not connect to the LLM provider")
        return LLMAPIError(f"Unexpected LLM client failure ({type(exc).__name__})")

    def _call(self, fn, **kwargs):
        import openai
        try:
            return fn(**kwargs)
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError):
            raise  # unusable *output*, not an API failure: generate_structured retries once
        except openai.OpenAIError as exc:
            raise self._translate(exc) from None

    # ---- public API -------------------------------------------------------------
    def generate_text(self, system: str, user: str, *, max_output_tokens: Optional[int] = None) -> LLMResult:
        started = time.perf_counter()
        if self.mock:
            return LLMResult(self.mock_text, MOCK_MODEL_NAME, 1, 0, 0, 0, _ms(started), mock=True)
        kwargs: dict[str, Any] = dict(
            model=self.config.model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens
        response = self._call(self._sdk_client().responses.create, **kwargs)
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise LLMOutputError("The LLM returned no text")
        inp, out, total = self._usage(response)
        result = LLMResult(text, self.config.model, 1, inp, out, total, _ms(started))
        logger.info("llm_text model=%s tokens=%d latency_ms=%d", result.model, total, result.latency_ms)
        return result

    def generate_structured(self, system: str, user: str, schema: Type[T], *,
                            max_output_tokens: Optional[int] = None) -> StructuredResult:
        """Return an instance of ``schema``. At most one retry on invalid output."""
        started = time.perf_counter()
        if self.mock:
            return StructuredResult(mock_instance(schema), MOCK_MODEL_NAME, 1, 0, 0, 0, _ms(started), mock=True)

        import openai
        messages: list[dict[str, str]] = [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]
        calls = 0
        tot_in = tot_out = tot = 0
        last_problem = "unknown"
        for attempt in range(2):  # first try + at most ONE retry
            kwargs: dict[str, Any] = dict(model=self.config.model, input=messages, text_format=schema)
            if max_output_tokens:
                kwargs["max_output_tokens"] = max_output_tokens
            calls += 1
            try:
                response = self._call(self._sdk_client().responses.parse, **kwargs)
            except LLMError:
                raise  # timeouts / API errors are not "invalid output": no retry here
            except (ValidationError, ValueError) as exc:  # parse(): bad JSON or schema mismatch
                last_problem = f"schema validation failed ({type(exc).__name__})"
                messages = messages[:2] + [{"role": "user", "content": CORRECTION_MESSAGE}]
                continue
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
                last_problem = f"output unusable ({type(exc).__name__})"
                messages = messages[:2] + [{"role": "user", "content": CORRECTION_MESSAGE}]
                continue
            inp, out, total = self._usage(response)
            tot_in, tot_out, tot = tot_in + inp, tot_out + out, tot + total
            parsed = getattr(response, "output_parsed", None)
            if isinstance(parsed, schema):
                result = StructuredResult(parsed, self.config.model, calls, tot_in, tot_out, tot,
                                          _ms(started), retried=attempt == 1)
                logger.info("llm_structured model=%s calls=%d tokens=%d latency_ms=%d",
                            result.model, calls, tot, result.latency_ms)
                return result
            last_problem = "model refused or returned no parsable output"
            messages = messages[:2] + [{"role": "user", "content": CORRECTION_MESSAGE}]
        raise LLMOutputError(f"The LLM did not return valid structured output after one retry ({last_problem})")


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
