# Deploying

## A. One free app on Streamlit Community Cloud (recommended; no separate backend)

The Streamlit app runs the whole backend **inside itself**: when the `EMBEDDED_API` secret is on, `ui/app.py` builds the SQLite
database and the document index, starts the FastAPI app on `127.0.0.1` in a background thread, and the UI calls it through the normal
HTTP client. The API is bound to loopback, so it is **not reachable from the internet**; only the app can call it. No `API_BASE_URL`
is needed in this mode.

### Steps
1. https://share.streamlit.io -> **Create app** -> *Deploy a public app from GitHub*.
   * Repository: `tarunyadav13/CodeForge` (your Streamlit account must have access to it; if it is not your repo, fork it first)
   * Branch: **`deploy/render`** (it holds this work; any branch containing these files works, including `main` after a merge)
   * Main file path: **`ui/app.py`**
2. **Advanced settings**
   * Python version: **3.13** (the version I tested locally is 3.13.1; 3.12 is likely fine but untested).
   * **Secrets** (TOML):
     ```toml
     EMBEDDED_API = "1"

     # optional: lets an LLM phrase the answers; without it the app quotes the cited text and says so
     # OPENAI_API_KEY = "sk-..."
     ```
3. **Deploy.** The first start takes a minute or two: it installs `ui/requirements.txt`, downloads the embedding model (~80 MB), and
   builds the database and index. The sidebar then shows `API ok`, `Vector store ok`, `SQLite ok` (LLM `not_configured` without a key).
4. Try: *"What is the minimum attendance of classes held in a course to appear in the end-semester examination?"* (cited answer), enter
   `S1002` in the sidebar and ask *"What is my attendance in CS201?"* (tool answer, 77.5%).

### What to know
* **Everything is rebuilt whenever the app restarts or wakes from sleep.** Storage is a temp directory; documents ingested through the UI
  and audit records are lost then. Only the synthetic students and the dated documents in `data/source_register.csv` come back.
  (The three real PDFs are not loaded: their effective dates are unknown.)
* **Memory.** Embeddings use the ONNX build of `all-MiniLM-L6-v2` that ships with `chromadb` (no `torch`). Checked: vectors identical to
  `sentence-transformers` (cosine 1.00000 on 4 texts, same query-to-document similarities); process memory 114 MB vs 567 MB for the
  embedder alone. The whole app (Streamlit script runtime + API + database + index + model) measured **about 395 MB in one process**
  on Windows. I did not look up Community Cloud's current memory limit; compare it with that number and the app's resource graph.
* **Dependencies.** `ui/requirements.txt` is the Cloud set (no torch, no pytest). The root `requirements.txt` is the full local set.
  Check the build log to confirm Cloud used `ui/requirements.txt`; if it used the root file instead the app still runs (the ONNX path
  never imports torch) but installs more.
* **Security.** The "login" is the student ID typed in the sidebar (a prototype identity, labelled in the UI), so anyone using the
  public app can enter any ID. The data is synthetic; never load real student data here. Isolation inside the API (tools locked to the
  header identity, other students refused, no question text in audit logs) is unchanged.
* One worker only: the app runs a single embedded Uvicorn thread.

### Verified vs not
Verified on this machine: the Streamlit script run with `EMBEDDED_API=1`, no `API_BASE_URL`, empty storage: the app built everything,
started the loopback API and answered a cited policy question, a personal-record question (S1002) and an unanswerable one
(`Retrieved fact` / `Calculated` / `Not found`); 24 offline tests in `tests/test_deploy_config.py` (embedded mode on and off, loopback
API with real bootstrap data, requirements, blueprint). **Not verified:** anything on Streamlit Community Cloud itself (no access from
here): the Python version choice, the build, the memory limit, sleep/wake behaviour, that it reads `ui/requirements.txt`.

## B. Separate UI and API (the UI has no default API URL)

Without `EMBEDDED_API`, the UI calls a separate API at **`API_BASE_URL`** (Streamlit secret or environment variable, `https://`, no
trailing slash; also editable in the sidebar). If it is unset, the UI makes no request and shows how to configure it. No CORS
configuration is needed: the browser never calls the API, Streamlit's server does.

### Optional backend host: Render (paid)
`render.yaml` (branch `deploy/render`): build `pip install --upgrade pip && pip install -r requirements-api.txt`; start
`python scripts/bootstrap_deploy.py && uvicorn api.main:app --host 0.0.0.0 --port $PORT --workers 1`; health check `/health`;
`standard` plan with a 2 GB disk at `/var/data` (SQLite, ChromaDB, model cache persist there; persistent disks are a paid-plan
feature, not verified by me). It uses the `sentence-transformers` embedder, measured at **629 MB** resident (145 MB at start), above
the free plan's 512 MB, which is why it needs a paid plan. Setting `EMBEDDING_BACKEND=onnx` there would cut that (see section A), but I
did not measure the API-only process with it. Secrets (`OPENAI_API_KEY`, `AUDIT_ADMIN_TOKEN`) go only in the Render dashboard.
Not verified on Render itself.

## Persistence summary
| Deployment | SQLite / audit | ChromaDB index | Survives restart? |
|---|---|---|---|
| A. Streamlit Cloud, embedded | temp dir | temp dir | **No**: rebuilt on each start |
| B. Render with the disk | `/var/data` | `/var/data` | Yes (if the disk works as documented) |
| Local | `data/student_services.db` | `data/chroma` | Yes |
