"""Deployment configuration: the UI's API URL, the backend requirements, the Render blueprint and the boot bootstrap.

Offline. Nothing here deploys anything; it checks that the files describe what the code needs and that the boot step is
idempotent. It does not prove the app runs on Render.
"""
import ast
import csv
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui.api_client import NOT_CONFIGURED_MESSAGE, ApiClient, get_api_base_url  # noqa: E402


class ExplodingSession:
    def request(self, *a, **k):
        raise AssertionError("no HTTP call may be made without a configured API URL")


# ---- the UI's API URL --------------------------------------------------------------------------------------------------
def test_no_default_api_url_and_no_call_when_unconfigured(monkeypatch):
    monkeypatch.delenv("API_BASE_URL", raising=False)
    assert get_api_base_url() == ""
    r = ApiClient(session=ExplodingSession()).health()
    assert not r.ok and "not configured" in r.error and "API_BASE_URL" in r.error and NOT_CONFIGURED_MESSAGE in r.error


def test_api_url_comes_from_the_environment_without_trailing_slash_or_spaces(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "  https://api.example.com/  ")
    assert get_api_base_url() == "https://api.example.com"
    assert ApiClient().base_url == "https://api.example.com"


def test_api_url_must_be_http_or_https():
    r = ApiClient("api.example.com", session=ExplodingSession()).health()
    assert not r.ok and "http://" in r.error


def test_connection_error_hints_when_the_url_is_localhost():
    import requests

    class Refuses:
        def request(self, *a, **k):
            raise requests.exceptions.ConnectionError()

    local = ApiClient("http://localhost:8000", session=Refuses()).health()
    remote = ApiClient("https://api.example.com", session=Refuses()).health()
    assert "machine running this UI" in local.error and "machine running this UI" not in remote.error


def test_streamlit_secret_is_used_and_the_sidebar_shows_it(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("API_BASE_URL", raising=False)
    at = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=60)
    at.secrets["API_BASE_URL"] = "https://secret-api.example.com"
    at.run()
    assert not at.exception
    assert any(t.value == "https://secret-api.example.com" for t in at.sidebar.text_input)


def test_unconfigured_ui_shows_the_instruction_not_a_localhost_error(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("API_BASE_URL", raising=False)
    at = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=60).run()
    assert not at.exception
    shown = " ".join(e.value for e in at.error)
    assert "not configured" in shown and "localhost:8000" not in shown.replace("http://127.0.0.1:8000", "")


def test_the_ui_package_has_no_default_api_url_constant():
    text = (ROOT / "ui" / "api_client.py").read_text(encoding="utf-8")
    assert "DEFAULT_API_BASE_URL" not in text
    assert 'getenv("API_BASE_URL", ' not in text  # no second argument = no fallback value


# ---- requirements ---------------------------------------------------------------------------------------------------------
BACKEND_PACKAGES = ["api", "db", "tools", "graph", "rag", "llm", "scripts"]
DIST = {"fastapi": "fastapi", "starlette": "fastapi", "uvicorn": "uvicorn", "pydantic": "pydantic", "openai": "openai",
        "langgraph": "langgraph", "chromadb": "chromadb", "sentence_transformers": "sentence-transformers",
        "pypdf": "pypdf", "requests": "requests"}


def _requirement_names(path):
    names = set()
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            for sep in ("[", ">", "<", "=", "~", "!"):
                line = line.split(sep)[0]
            names.add(line.strip().lower())
    return names


def _third_party_imports():
    first_party = set(BACKEND_PACKAGES) | {"ui", "eval", "tests"}
    found = set()
    for pkg in BACKEND_PACKAGES:
        for f in (ROOT / pkg).rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                else:
                    mods = []
                for m in mods:
                    top = m.split(".")[0]
                    if top not in first_party and top not in sys.stdlib_module_names and top != "__future__":
                        found.add(top)
    return found


def test_backend_requirements_cover_every_third_party_import_of_the_backend():
    declared = _requirement_names("requirements-api.txt")
    missing = {m for m in _third_party_imports() if m not in DIST or DIST[m] not in declared}
    assert not missing, f"imported by backend code but not in requirements-api.txt (or not mapped): {sorted(missing)}"


def test_backend_requirements_exclude_ui_and_test_only_packages():
    assert not _requirement_names("requirements-api.txt") & {"streamlit", "pytest", "httpx"}


def test_backend_requirements_are_a_subset_of_the_full_requirements():
    assert _requirement_names("requirements-api.txt") <= _requirement_names("requirements.txt")


def test_streamlit_cloud_requirements_run_the_whole_app_without_torch():
    """ui/requirements.txt is what Streamlit Community Cloud installs for the one-app (embedded API) deployment."""
    ui = _requirement_names("ui/requirements.txt")
    assert {"streamlit", "requests"} <= ui
    assert not ui & {"torch", "sentence-transformers", "pytest", "httpx"}
    missing = {m for m in _third_party_imports() if m not in DIST or DIST[m] not in ui}
    # sentence_transformers is imported lazily and only for the non-ONNX backend, which the embedded mode never selects
    assert missing <= {"sentence_transformers"}, sorted(missing)


# ---- Render blueprint -------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def service():
    return yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]


def test_blueprint_start_command_runs_bootstrap_then_one_uvicorn_worker_on_render_port(service):
    cmd = service["startCommand"]
    assert cmd.startswith("python scripts/bootstrap_deploy.py && uvicorn api.main:app")
    assert "--host 0.0.0.0" in cmd and "--port $PORT" in cmd and "--workers 1" in cmd and "--reload" not in cmd
    assert (ROOT / "scripts" / "bootstrap_deploy.py").is_file() and service["healthCheckPath"] == "/health"
    assert "requirements-api.txt" in service["buildCommand"]


def test_blueprint_persists_data_on_a_disk_and_is_not_the_free_plan(service):
    env = {e["key"]: e for e in service["envVars"]}
    assert service["plan"] != "free"  # measured ~629 MB resident > 512 MB; persistent disks need a paid plan
    mount = service["disk"]["mountPath"]
    for key in ("STUDENT_DB_PATH", "CHROMA_DIR", "HF_HOME"):
        assert env[key]["value"].startswith(mount + "/"), key


def test_blueprint_contains_no_secret_values_and_no_localhost(service):
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    env = {e["key"]: e for e in service["envVars"]}
    assert env["OPENAI_API_KEY"].get("sync") is False and "value" not in env["OPENAI_API_KEY"]
    assert env["AUDIT_ADMIN_TOKEN"].get("generateValue") is True and "value" not in env["AUDIT_ADMIN_TOKEN"]
    assert "sk-" not in text and "localhost" not in text and "MOCK_LLM" not in env


def test_no_cors_middleware_is_needed_because_the_ui_calls_from_its_server():
    assert "CORSMiddleware" not in (ROOT / "api" / "main.py").read_text(encoding="utf-8")


# ---- bootstrap ------------------------------------------------------------------------------------------------------------
def _register_rows():
    with open(ROOT / "data" / "source_register.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_bootstrap_builds_students_and_corpus_and_a_second_run_does_nothing(tmp_path):
    from fastapi.testclient import TestClient

    from api.main import create_app
    from rag import HashEmbedder, VectorStore
    from rag import service as rag_service
    from scripts import bootstrap_deploy as bd

    db = tmp_path / "boot.db"
    rag_service.set_store(VectorStore(tmp_path / "chroma", HashEmbedder()))
    try:
        assert "loaded" in bd.ensure_students(db)
        first = bd.ensure_corpus(db)
        dated = [r["doc_id"] for r in _register_rows() if r["effective_from"].strip()]
        undated = [r["doc_id"] for r in _register_rows() if not r["effective_from"].strip()]
        assert dated and all(any(d in line and line.startswith("ok") and "already" not in line for line in first) for d in dated)
        assert undated and all(any(d in line and line.startswith("skip") for line in first) for d in undated)  # dates not guessed
        assert "already loaded" in bd.ensure_students(db)
        assert all("already indexed" in line for line in bd.ensure_corpus(db) if line.startswith("ok"))
        with TestClient(create_app(db, graph_runner=None, upload_dir=tmp_path / "up")) as c:
            assert set(dated) <= {s["doc_id"] for s in c.get("/sources").json()}  # SQLite register matches the vector store
            assert c.get("/health").json()["vector_store"]["status"] == "ok"
    finally:
        rag_service.set_store(None)


def test_bootstrap_repairs_a_vector_store_that_lost_its_chunks(tmp_path):
    from rag import HashEmbedder, VectorStore
    from rag import service as rag_service
    from scripts import bootstrap_deploy as bd

    db = tmp_path / "boot.db"
    rag_service.set_store(VectorStore(tmp_path / "chroma1", HashEmbedder()))
    try:
        bd.ensure_students(db)
        bd.ensure_corpus(db)
        rag_service.set_store(VectorStore(tmp_path / "chroma2", HashEmbedder()))  # SQLite still says indexed; chunks are gone
        again = bd.ensure_corpus(db)
        assert any(line.startswith("ok") and "indexed" in line and "already" not in line for line in again)
        assert rag_service.get_store().chunk_count() > 0
    finally:
        rag_service.set_store(None)


def test_bootstrap_tolerates_a_corpus_failure_unless_strict(tmp_path, monkeypatch):
    from scripts import bootstrap_deploy as bd

    monkeypatch.setenv("STUDENT_DB_PATH", str(tmp_path / "b.db"))

    def boom(*a, **k):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(bd, "ensure_corpus", boom)
    assert bd.main([]) == 0  # the API can still start and serve personal-record tools
    assert bd.main(["--strict"]) == 1


def test_bootstrap_fails_loudly_when_the_student_database_cannot_be_prepared(monkeypatch):
    from scripts import bootstrap_deploy as bd

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bd, "ensure_students", boom)
    assert bd.main([]) == 1


# ---- one-app hosting: the backend inside the Streamlit process --------------------------------------------------------------
def test_embedded_mode_is_opt_in_and_parsed_strictly():
    from ui.embedded_api import embedded_enabled

    assert [embedded_enabled(v) for v in ("1", "true", "YES", "on")] == [True] * 4
    assert [embedded_enabled(v) for v in ("", "0", "false", "no", "maybe")] == [False] * 5


def test_embedded_mode_defaults_go_to_a_temp_dir_use_the_onnx_embedder_and_never_override_settings(tmp_path, monkeypatch):
    from ui.embedded_api import configure_environment

    for k in ("STUDENT_DB_PATH", "CHROMA_DIR", "HF_HOME", "EMBEDDING_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    base = configure_environment(tmp_path / "app")
    import os

    assert os.environ["STUDENT_DB_PATH"].startswith(str(base)) and os.environ["CHROMA_DIR"].startswith(str(base))
    assert os.environ["EMBEDDING_BACKEND"] == "onnx"
    monkeypatch.setenv("CHROMA_DIR", "/custom/chroma")
    configure_environment(tmp_path / "app")
    assert os.environ["CHROMA_DIR"] == "/custom/chroma"


def test_onnx_embedder_is_selected_by_name_loads_lazily_and_embeds_nothing_for_empty_input():
    from rag.embeddings import OnnxMiniLMEmbedder, get_embedder

    e = get_embedder({"EMBEDDING_BACKEND": "onnx"})
    assert isinstance(e, OnnxMiniLMEmbedder) and e.name == "onnx-all-MiniLM-L6-v2" and e._fn is None
    assert e.embed([]) == [] and e._fn is None  # no model download for nothing


def test_the_embedded_api_listens_on_loopback_only_and_answers_with_real_data(tmp_path, monkeypatch):
    import requests

    from rag import service as rag_service
    from scripts import bootstrap_deploy as bd
    from ui.embedded_api import start_embedded_api

    monkeypatch.setenv("STUDENT_DB_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")  # offline stand-in; the ONNX model needs a download
    monkeypatch.setenv("RAG_MIN_SCORE", "0.2")
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.delenv("API_BASE_URL", raising=False)
    rag_service.set_store(None)

    def build():
        bd.ensure_students()
        bd.ensure_corpus()

    try:
        url = start_embedded_api(bootstrap=build, ready_timeout=120)
        assert url.startswith("http://127.0.0.1:")  # loopback: not reachable from outside the host
        h = requests.get(f"{url}/health", timeout=10).json()
        assert h["sqlite"]["status"] == "ok" and h["vector_store"]["status"] == "ok"
        r = requests.post(f"{url}/ask", headers={"X-Student-Id": "S1002"}, timeout=60,
                          json={"question": "What is my attendance in CS201?", "as_of_date": "2026-10-06"}).json()
        assert r["answer_type"] == "calculated" and "77.5" in r["answer"]
    finally:
        rag_service.set_store(None)


def test_app_starts_the_embedded_api_when_enabled_and_needs_no_api_url(monkeypatch):
    """The Streamlit script, run for real with EMBEDDED_API on, must end up pointing at its own loopback API."""
    from streamlit.testing.v1 import AppTest

    import ui.embedded_api as ea

    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.setenv("EMBEDDED_API", "1")
    monkeypatch.setattr(ea, "start_embedded_api", lambda *a, **k: "http://127.0.0.1:59999")
    at = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert any(t.value == "http://127.0.0.1:59999" for t in at.sidebar.text_input)
