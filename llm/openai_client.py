"""OpenAI implementation of ``LLMClient`` (official SDK, Responses API).

Configuration (environment variables only; nothing is hard-coded or stored):

    OPENAI_API_KEY            required for real calls. Read once and handed to the SDK;
                              this class never keeps, prints or logs it.
    OPENAI_MODEL              default "gpt-6-luna"  (efficient tier; see llm/README.md)
    OPENAI_TIMEOUT_S          default 30            per-request timeout, seconds
    OPENAI_MAX_RETRIES        default 1             SDK-level retries for connection errors,
                                                    429 and 5xx. Not output retries.
    OPENAI_MAX_OUTPUT_TOKENS  default 2000          includes hidden reasoning tokens
    OPENAI_REASONING_EFFORT   default "low"         none|low|medium|high|xhigh|max, or
                                                    "off" to omit the parameter (needed for
                                                    models that are not reasoning models)

Behaviour
    * One request per ``generate`` call. If the OUTPUT is unusable (empty, or JSON that
      does not match ``response_model``) exactly ONE corrective retry is made. Refusals,
      truncated (incomplete) responses and API/transport errors are never retried here.
    * Failures raise ``LLMError`` subclasses carrying only safe messages. A failure is
      never converted into a mock answer.
    * ``store=False``: responses are not retained by OpenAI's response storage.
    * No ``temperature``: reasoning models do not accept it, so none is sent.
    * Prompts, model output and the API key are never logged.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping, Optional, Type

from pydantic import ValidationError

from llm.client import (
    OPENAI_PROVIDER,
    LLMAPIError,
    LLMConfigError,
    LLMError,
    LLMHealth,
    LLMOutputError,
    LLMResult,
    LLMTimeoutError,
    LLMUsage,
    T,
    _validation_summary,
    scrub,
)

logger = logging.getLogger("llm")

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_OUTPUT_TOKENS = 2000
DEFAULT_REASONING_EFFORT = "low"
VALID_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_OMIT_EFFORT = {"off", "omit"}


# --------------------------------------------------------------------------- env parsing
def _env_number(env: Mapping[str, str], name: str, default: float, *, cast, minimum: float, maximum: float):
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = cast(str(raw).strip())
    except ValueError:
        raise LLMConfigError(f"{name} must be a number (got {scrub(raw)!r}).") from None
    if not (minimum <= value <= maximum):
        raise LLMConfigError(f"{name} must be between {minimum} and {maximum} (got {value}).")
    return value


def _parse_effort(raw: Optional[str]) -> Optional[str]:
    if raw is None or not str(raw).strip():
        return DEFAULT_REASONING_EFFORT
    value = str(raw).strip().lower()
    if value in _OMIT_EFFORT:
        return None
    if value not in VALID_REASONING_EFFORTS:
        raise LLMConfigError(
            "OPENAI_REASONING_EFFORT must be one of "
            f"{', '.join(VALID_REASONING_EFFORTS)} or 'off' (got {scrub(raw)!r})."
        )
    return value


# --------------------------------------------------------------------------- error mapping
_SAFE_MESSAGES = {
    "timeout": "The language model request timed out.",
    "rate_limit": "The language model API rate limit or quota was reached.",
    "auth": "The language model API rejected the credentials (check OPENAI_API_KEY).",
    "bad_request": "The language model API rejected the request (check OPENAI_MODEL and settings).",
    "not_found": "The configured model was not found or is not accessible (check OPENAI_MODEL).",
    "server": "The language model service reported a server error.",
    "connection": "Could not connect to the language model API.",
    "other": "The language model API returned an error.",
}


def _translate(exc: BaseException) -> Optional[LLMError]:
    """Map an OpenAI SDK exception to a safe ``LLMError``; None if it is not an SDK error.

    Matching is by class name along the MRO, so it needs no import of ``openai`` and
    unexpected programming errors (TypeError, ...) are NOT swallowed.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "OpenAIError" not in names:
        return None
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    detail = str(getattr(exc, "message", None) or exc)

    if "APITimeoutError" in names:
        kind = "timeout"
    elif "RateLimitError" in names:
        kind = "rate_limit"
    elif "AuthenticationError" in names or "PermissionDeniedError" in names:
        kind = "auth"
    elif "BadRequestError" in names or "UnprocessableEntityError" in names:
        kind = "bad_request"
    elif "NotFoundError" in names:
        kind = "not_found"
    elif "InternalServerError" in names or (isinstance(status, int) and status >= 500):
        kind = "server"
    elif "APIConnectionError" in names:
        kind = "connection"
    else:
        kind = "other"

    message = _SAFE_MESSAGES[kind]
    if status:
        message = f"{message} (HTTP {status})"
    cls = LLMTimeoutError if kind == "timeout" else LLMAPIError
    return cls(message, kind=kind, status_code=status, request_id=request_id, detail=detail)


# --------------------------------------------------------------------------- response parsing
class _Invalid:
    """Output that is unusable but worth exactly one corrective retry."""

    def __init__(self, kind: str, summary: str) -> None:
        self.kind = kind
        self.summary = summary


def _usage_of(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage()

    def _n(name: str) -> int:
        try:
            return int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return LLMUsage(_n("input_tokens"), _n("output_tokens"), _n("total_tokens"))


def _find_refusal(response: Any) -> Optional[str]:
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return str(getattr(part, "refusal", "") or "")
    return None


def _correction_message(invalid: _Invalid, structured: bool) -> str:
    if structured:
        return (
            f"Your previous reply could not be used ({invalid.summary}). "
            "Reply again with ONLY a JSON object that matches the required schema."
        )
    return "Your previous reply was empty. Reply again with the answer text."


# --------------------------------------------------------------------------- client
class OpenAIClient:
    provider = OPENAI_PROVIDER

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        reasoning_effort: Optional[str] = DEFAULT_REASONING_EFFORT,
        sdk_client: Any = None,
    ) -> None:
        """``sdk_client`` lets tests inject a fake; production code leaves it None."""
        self.model = (model or DEFAULT_MODEL).strip()
        self._max_output_tokens = int(max_output_tokens)
        self._reasoning_effort = reasoning_effort
        if reasoning_effort is not None and reasoning_effort not in VALID_REASONING_EFFORTS:
            raise LLMConfigError(f"Invalid reasoning effort {scrub(reasoning_effort)!r}.")
        self._schemas: dict[type, tuple[str, dict]] = {}

        if sdk_client is not None:
            self._sdk = sdk_client
            return

        key = (api_key or "").strip()
        if not key:
            raise LLMConfigError(
                "OPENAI_API_KEY is not set. Set it in your environment (never in code or git), "
                "or set MOCK_LLM=true for offline use."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMConfigError("The 'openai' package is not installed. Run: pip install openai") from exc
        # The key goes straight to the SDK; this object keeps no copy of it.
        self._sdk = OpenAI(api_key=key, timeout=timeout_s, max_retries=max_retries)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "OpenAIClient":
        return cls(
            api_key=env.get("OPENAI_API_KEY"),
            model=(env.get("OPENAI_MODEL") or "").strip() or None,
            timeout_s=_env_number(env, "OPENAI_TIMEOUT_S", DEFAULT_TIMEOUT_S, cast=float, minimum=1, maximum=600),
            max_retries=_env_number(env, "OPENAI_MAX_RETRIES", DEFAULT_MAX_RETRIES, cast=int, minimum=0, maximum=5),
            max_output_tokens=_env_number(
                env, "OPENAI_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, cast=int, minimum=64, maximum=128000
            ),
            reasoning_effort=_parse_effort(env.get("OPENAI_REASONING_EFFORT")),
        )

    def __repr__(self) -> str:
        return f"OpenAIClient(model={self.model!r})"

    # ------------------------------------------------------------------ public API
    def generate(
        self, *, system: str, user: str, response_model: Optional[Type[T]] = None
    ) -> LLMResult:
        started = time.perf_counter()
        messages: list[dict[str, str]] = [{"role": "user", "content": user}]
        usage = LLMUsage()
        calls = 0
        response_id: Optional[str] = None

        for attempt in range(2):  # first try + at most ONE retry, only for unusable output
            calls += 1
            try:
                response = self._request(system, messages, response_model)
            except LLMError as err:
                self._fail(err, usage, calls)
                raise
            except Exception as exc:  # SDK errors -> controlled errors; real bugs propagate
                err = _translate(exc)
                if err is None:
                    raise
                self._fail(err, usage, calls)
                raise err from exc

            usage = usage + _usage_of(response)
            response_id = getattr(response, "id", None) or response_id

            try:
                outcome = self._interpret(response, response_model)
            except LLMError as err:  # refusal / incomplete / failed: terminal, no retry
                self._fail(err, usage, calls)
                raise

            if not isinstance(outcome, _Invalid):
                text, parsed = outcome
                latency_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "llm_call provider=%s model=%s calls=%d latency_ms=%d input_tokens=%d output_tokens=%d",
                    self.provider, self.model, calls, latency_ms, usage.input_tokens, usage.output_tokens,
                )
                return LLMResult(
                    text=text,
                    parsed=parsed,
                    provider=self.provider,
                    model=self.model,
                    usage=usage,
                    latency_ms=latency_ms,
                    llm_calls=calls,
                    response_id=response_id,
                )

            if attempt == 0:
                messages = messages + [
                    {"role": "user", "content": _correction_message(outcome, response_model is not None)}
                ]
                continue

            err = LLMOutputError(
                "The language model did not return usable output after one retry.",
                kind=outcome.kind,
                detail=outcome.summary,
            )
            self._fail(err, usage, calls)
            raise err

        raise AssertionError("unreachable")  # pragma: no cover

    def health(self) -> LLMHealth:
        """Real check: retrieves the configured model's metadata (no tokens billed)."""
        try:
            self._sdk.models.retrieve(self.model)
        except Exception as exc:  # health must never raise
            err = _translate(exc)
            detail = f"{err} [{err.kind}]" if err else f"unexpected {type(exc).__name__}"
            return LLMHealth("down", self.provider, self.model, detail)
        return LLMHealth("ok", self.provider, self.model, "Model metadata request succeeded.")

    # ------------------------------------------------------------------ internals
    def _schema_for(self, response_model: type) -> tuple[str, dict]:
        cached = self._schemas.get(response_model)
        if cached:
            return cached
        try:
            import openai
        except ImportError as exc:
            raise LLMConfigError("The 'openai' package is not installed. Run: pip install openai") from exc
        # Public SDK helper that produces a Structured-Outputs-strict schema
        # (every property required, additionalProperties false, defaults stripped).
        tool = openai.pydantic_function_tool(response_model, name=response_model.__name__)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", response_model.__name__)[:64]
        self._schemas[response_model] = (name, tool["function"]["parameters"])
        return self._schemas[response_model]

    def _request(self, system: str, messages: list[dict[str, str]], response_model: Optional[type]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": messages,
            "max_output_tokens": self._max_output_tokens,
            "store": False,
        }
        if self._reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}
        if response_model is not None:
            name, schema = self._schema_for(response_model)
            kwargs["text"] = {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}
        return self._sdk.responses.create(**kwargs)

    def _interpret(self, response: Any, response_model: Optional[type]):
        status = getattr(response, "status", None)
        if status == "failed":
            error = getattr(response, "error", None)
            raise LLMAPIError(
                "The language model reported a failed response.",
                kind="response_failed",
                detail=str(getattr(error, "message", "") or ""),
            )
        if status == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None) or "unknown"
            raise LLMOutputError(
                f"The language model response was cut off ({reason}). "
                "Consider raising OPENAI_MAX_OUTPUT_TOKENS.",
                kind="incomplete",
            )
        if status not in (None, "completed"):
            raise LLMAPIError(
                f"The language model response ended with status '{scrub(str(status))}'.",
                kind="response_failed",
            )
        if _find_refusal(response) is not None:
            raise LLMOutputError("The language model declined to answer.", kind="refusal")

        text = getattr(response, "output_text", None) or ""
        if not str(text).strip():
            return _Invalid("empty", "empty output")
        if response_model is None:
            return text, None
        try:
            parsed = response_model.model_validate_json(text)
        except ValidationError as exc:
            return _Invalid("invalid", _validation_summary(exc))
        return text, parsed

    def _fail(self, err: LLMError, usage: LLMUsage, calls: int) -> None:
        err.usage = usage
        err.llm_calls = calls
        logger.warning(
            "llm_failure provider=%s model=%s kind=%s status=%s request_id=%s calls=%d",
            self.provider, self.model, err.kind, err.status_code, err.request_id, calls,
        )
