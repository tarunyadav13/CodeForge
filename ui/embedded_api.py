"""Run the FastAPI backend INSIDE the Streamlit process, for one-app hosting (Streamlit Community Cloud).

Enabled by the ``EMBEDDED_API`` Streamlit secret / environment variable (``1``/``true``). Not enabled by default, so a
normal deployment keeps calling a separate API (``API_BASE_URL``).

What it does, once per server process (``st.cache_resource`` in ui/app.py):
  1. points SQLite, ChromaDB and the model cache at a temp directory (a Cloud checkout is not a place to persist data),
  2. uses the torch-free ONNX embedder (same MiniLM vectors, ~115 MB instead of ~570 MB resident),
  3. builds the database and the corpus (``scripts/bootstrap_deploy.py``: synthetic students + dated documents),
  4. starts Uvicorn on 127.0.0.1 in a daemon thread and waits for ``/health``.
The API is bound to loopback, so it is NOT reachable from the internet: only this app can call it. The UI then talks to
it through the normal HTTP client, so the code path is the same as with a remote API.

Everything is rebuilt whenever the app restarts or wakes from sleep: documents ingested through the UI and audit records
do not survive that. Student data is synthetic.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
_TRUE = {"1", "true", "yes", "on"}
_SECRETS_TO_ENV = ("OPENAI_API_KEY", "OPENAI_MODEL", "AUDIT_ADMIN_TOKEN", "MOCK_LLM")


def embedded_enabled(value: Optional[str] = None) -> bool:
    """True when EMBEDDED_API is set (argument, else Streamlit secret, else environment)."""
    if value is None:
        try:
            import streamlit as st

            value = st.secrets.get("EMBEDDED_API")
        except Exception:  # noqa: BLE001 - no secrets file
            value = None
        value = value if value is not None else os.getenv("EMBEDDED_API")
    return str(value or "").strip().lower() in _TRUE


def configure_environment(base_dir: Optional[Path] = None) -> Path:
    """Defaults for a small ephemeral host; anything already set (secrets / environment) wins."""
    base = Path(base_dir or Path(tempfile.gettempdir()) / "student_services_app")
    base.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("STUDENT_DB_PATH", str(base / "student_services.db"))
    os.environ.setdefault("CHROMA_DIR", str(base / "chroma"))
    os.environ.setdefault("HF_HOME", str(base / "huggingface"))
    os.environ.setdefault("EMBEDDING_BACKEND", "onnx")
    return base


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _forward_secrets_to_environment() -> None:
    """Streamlit exports top-level secrets as env vars on Cloud; do it explicitly for the ones the backend reads."""
    try:
        import streamlit as st

        for key in _SECRETS_TO_ENV:
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except Exception:  # noqa: BLE001 - no secrets configured
        pass


def start_embedded_api(bootstrap: Optional[Callable[[], None]] = None, ready_timeout: float = 600.0) -> str:
    """Build the data, start Uvicorn on loopback in a thread, return its base URL. Raises RuntimeError on failure."""
    import urllib.request

    configure_environment()
    _forward_secrets_to_environment()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if bootstrap is None:
        from scripts import bootstrap_deploy

        def bootstrap() -> None:
            if bootstrap_deploy.main([]) != 0:
                raise RuntimeError("the student database could not be prepared")

    bootstrap()
    import uvicorn

    from api.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, name="embedded-api", daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3):
                return url
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.5)
    raise RuntimeError("the embedded API did not become ready")
