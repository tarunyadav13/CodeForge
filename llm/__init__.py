"""LLM layer. Two clients live here (see llm/README.md):

* ``llm.openai_client``    - ``LLMClient`` / ``LLMConfig`` / ``LLMError`` ... (Member 2: API /health, data generation).
* ``llm.client`` + ``llm.responses_client`` - ``get_llm`` / ``MockLLM`` / ``OpenAIClient`` ... (Member 1: RAG workflow).

Both define classes with the SAME names (``LLMError``, ``LLMResult``, ``LLMClient`` ...) that are different types.
The names re-exported here are Member 2's, unchanged, so existing callers keep working. Member 1's clashing names
must be imported from ``llm.client``; the names below that are unique to Member 1 are safe to import from ``llm``.
"""
from llm.client import LLMHealth, LLMUsage, MockLLM, get_llm, is_mock_enabled  # noqa: F401  (Member 1, unique names)
from llm.openai_client import (  # noqa: F401  (Member 2, unchanged)
    DEFAULT_MODEL,
    LLMAPIError,
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMError,
    LLMOutputError,
    LLMResult,
    LLMTimeoutError,
    StructuredResult,
)
