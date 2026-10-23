"""Streamlit UI for the University Student Services Assistant.

Run:  streamlit run ui/app.py      (API_BASE_URL defaults to http://localhost:8000)

The UI only displays what the FastAPI backend returns. It never builds an answer of
its own: when the API fails, the user sees the error, not a guess.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.api_client import (  # noqa: E402
    ApiClient,
    STUDENT_ID_HEADER,
    get_api_base_url,
    is_valid_student_id,
)
from ui.formatting import (  # noqa: E402
    STATUS_ICONS,
    answer_type_display,
    applied_rule_rows,
    citation_rows,
    conflict_is_unresolved,
    conflict_summary,
    kv_rows,
    safe_text,
)

st.set_page_config(page_title="Student Services Assistant", page_icon="🎓", layout="wide")


# --- State ------------------------------------------------------------------------------

def init_state() -> None:
    defaults = {
        "messages": [],          # {"role", "content", "response", "error", "warnings", "latency_ms"}
        "api_base_url": get_api_base_url(),
        "health": None,          # ApiResult of the last /health call
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def client() -> ApiClient:
    return ApiClient(st.session_state.api_base_url)


def refresh_health() -> None:
    st.session_state.health = client().health()


# --- Rendering of one /ask response -----------------------------------------------------

def render_badge(answer_type: str) -> None:
    label, color, meaning = answer_type_display(answer_type)
    if hasattr(st, "badge"):
        st.badge(label, color=color)
    else:  # Streamlit < 1.46 has no st.badge
        st.markdown(f":{color}-background[**{label}**]")
    st.caption(meaning)


def render_answer_body(resp: dict) -> None:
    answer_type = resp["answer_type"]
    answer = safe_text(resp["answer"])  # untrusted: derived from document text
    if answer_type == "refused":
        st.error(answer, icon="🚫")
    elif answer_type == "not_found":
        st.info(answer, icon="🔎")
    elif answer_type == "clarification_needed":
        st.warning(answer, icon="❓")
    elif answer_type == "conflict_flagged":
        st.warning(
            "**Unresolved policy conflict.** The sources below disagree and the precedence rules "
            "could not pick one. Treat this as not definitive and check with the university office.",
            icon="⚠️",
        )
        st.markdown(answer)
    else:
        st.markdown(answer)


def render_citations(citations: list[dict]) -> None:
    with st.expander(f"Citations ({len(citations)})", expanded=bool(citations)):
        if citations:
            st.dataframe(citation_rows(citations), hide_index=True, use_container_width=True)
        else:
            st.caption("No citations returned.")


def render_tools(tools: list[dict]) -> None:
    with st.expander(f"Tools invoked ({len(tools)})", expanded=bool(tools)):
        if not tools:
            st.caption("No deterministic tools were called.")
        for t in tools:
            st.markdown(f"**`{t['tool']}`**")
            col_in, col_out = st.columns(2)
            with col_in:
                st.caption("Input")
                if t["input"]:
                    st.dataframe(kv_rows(t["input"]), hide_index=True, use_container_width=True)
                else:
                    st.caption("—")
            with col_out:
                st.caption("Output (calculated by code, not the LLM)")
                if t["output"]:
                    st.dataframe(kv_rows(t["output"]), hide_index=True, use_container_width=True)
                else:
                    st.caption("—")


def render_rules(rules: list[dict]) -> None:
    with st.expander(f"Applied rules ({len(rules)})", expanded=bool(rules)):
        if rules:
            st.dataframe(applied_rule_rows(rules), hide_index=True, use_container_width=True)
        else:
            st.caption("No rules applied.")


def render_conflicts(conflicts: list[dict], answer_type: str) -> None:
    if not conflicts:
        return
    unresolved = [c for c in conflicts if conflict_is_unresolved(c, answer_type)]
    title = f"Conflicts detected ({len(conflicts)}"
    title += f", {len(unresolved)} unresolved)" if unresolved else ", resolved by precedence)"
    with st.expander(title, expanded=True):
        for c in conflicts:
            if conflict_is_unresolved(c, answer_type):
                st.warning(f"**Unresolved:** {safe_text(conflict_summary(c))}", icon="⚠️")
            else:
                st.info(f"**Resolved:** {safe_text(conflict_summary(c))}", icon="⚖️")
            st.json(c, expanded=False)


def render_response(resp: dict, warnings: list[str], latency_ms: float) -> None:
    render_badge(resp["answer_type"])
    render_answer_body(resp)
    if resp.get("explanation"):
        st.caption(f"Explanation: {safe_text(resp['explanation'])}")
    render_conflicts(resp["conflicts_detected"], resp["answer_type"])
    if resp["citations"]:
        render_citations(resp["citations"])
    elif resp["answer_type"] in ("retrieved_fact", "conflict_flagged"):
        st.warning("This answer came back without citations, so it cannot be checked against a source.", icon="⚠️")
    if resp["tools_invoked"]:
        render_tools(resp["tools_invoked"])
    if resp["applied_rules"]:
        render_rules(resp["applied_rules"])
    with st.expander("Trace"):
        st.caption("Trace ID (use it in the Audit tab)")
        st.code(resp["trace_id"], language=None)
        st.caption(f"As-of date: {resp['as_of_date']} · Round trip: {latency_ms:.0f} ms")
    if warnings:
        st.caption("⚠️ Contract warnings: " + "; ".join(warnings))


def render_message(msg: dict) -> None:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
            if msg.get("meta"):
                st.caption(msg["meta"])
        elif msg.get("error"):
            st.error(f"No answer: {msg['error']}", icon="🛑")
        else:
            render_response(msg["response"], msg.get("warnings", []), msg.get("latency_ms", 0.0))


# --- Sidebar ----------------------------------------------------------------------------

def render_sidebar() -> tuple[str, date | None]:
    with st.sidebar:
        st.header("Session")
        student_id = st.text_input(
            "Student ID",
            placeholder="e.g. S1001",
            help=f"Sent as the {STUDENT_ID_HEADER} header.",
        ).strip().upper()
        if student_id and not is_valid_student_id(student_id):
            st.warning("Student IDs look like S followed by 4 digits.")
        st.caption(
            f"Prototype identity: the ID is sent in the `{STUDENT_ID_HEADER}` header, as agreed for "
            "this hackathon. It is **not** real authentication. IDs typed into a question are ignored."
        )

        use_date = st.toggle("Ask as of a specific date", help="Policies are applied as they stood on this date.")
        as_of = st.date_input("As-of date", value=date.today(), disabled=not use_date) if use_date else None

        if st.button("New conversation", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

        st.divider()
        st.subheader("System status")
        st.session_state.api_base_url = st.text_input(
            "API base URL", value=st.session_state.api_base_url, help="Defaults to the API_BASE_URL env variable."
        ).rstrip("/")
        if st.button("Refresh status", use_container_width=True) or st.session_state.health is None:
            refresh_health()
        health = st.session_state.health
        if not health.ok:
            st.error(f"Health check failed: {health.error}")
        else:
            data = health.data
            st.markdown(f"**Overall:** {STATUS_ICONS.get(data['status'], '⚫')} {data['status']}")
            for name, label in (("api", "API"), ("vector_store", "Vector store"), ("sqlite", "SQLite"), ("llm", "LLM")):
                comp = data[name]
                st.markdown(f"{STATUS_ICONS.get(comp['status'], '⚫')} **{label}:** {comp['status']}")
                if comp.get("detail"):
                    st.caption(comp["detail"])
            if health.warnings:
                st.caption("⚠️ " + "; ".join(health.warnings))
    return student_id, as_of


# --- Tabs -------------------------------------------------------------------------------

def ask_tab(student_id: str, as_of: date | None) -> None:
    for msg in st.session_state.messages:
        render_message(msg)

    question = st.chat_input("Ask about university policies or your own records")
    if not question:
        return
    question = question.strip()
    meta = []
    meta.append(f"as student {student_id}" if student_id else "no student ID (policy questions only)")
    if as_of:
        meta.append(f"as of {as_of.isoformat()}")
    user_msg = {"role": "user", "content": question, "meta": " · ".join(meta)}
    st.session_state.messages.append(user_msg)
    render_message(user_msg)

    with st.chat_message("assistant"):
        with st.spinner("Searching sources and checking records…"):
            result = client().ask(question, student_id or None, as_of)
    msg = {"role": "assistant", "latency_ms": result.latency_ms}
    if result.ok:
        msg.update(response=result.data, warnings=result.warnings)
    else:
        msg.update(error=result.error)
    st.session_state.messages.append(msg)
    st.rerun()


def sources_tab() -> None:
    st.caption("Documents registered with the backend (GET /sources).")
    if st.button("Load sources"):
        result = client().sources()
        if not result.ok:
            st.error(result.error)
        elif not result.data:
            st.info("The backend reports no registered sources yet.")
        else:
            st.dataframe(result.data, hide_index=True, use_container_width=True)


def audit_tab() -> None:
    st.caption("Look up the audit record for a trace ID (GET /audit/{trace_id}).")
    with st.form("audit_form"):
        trace_id = st.text_input("Trace ID")
        submitted = st.form_submit_button("Look up")
    if submitted and trace_id.strip():
        result = client().audit(trace_id)
        if result.ok:
            st.json(result.data)
        else:
            st.error(result.error)


DOC_TYPES = ["regulation", "circular", "notice", "faq", "handbook", "unofficial"]


def ingest_tab() -> None:
    st.caption(
        "Add a document live (POST /ingest). Metadata follows the Source Register (Annex B). "
        "Results shown here come straight from the backend."
    )
    with st.form("ingest_form", clear_on_submit=False):
        upload = st.file_uploader("Document", type=["pdf", "txt", "md", "docx", "html"])
        c1, c2 = st.columns(2)
        with c1:
            doc_id = st.text_input("doc_id *")
            title = st.text_input("Title *")
            issuer = st.text_input("Issuer *")
            authority_level = st.number_input("Authority level * (1 = highest, 5 = informational)", 1, 5, 3)
            doc_type = st.selectbox("Document type *", DOC_TYPES)
            version = st.text_input("Version *")
            provenance = st.text_input("Provenance * (URL or origin)")
        with c2:
            effective_from = st.date_input("Effective from *", value=date.today())
            has_end = st.checkbox("Has an end date")
            effective_to = st.date_input("Effective to", value=date.today())
            supersedes = st.text_input("Supersedes (doc_id)")
            scope_programmes = st.text_input("Scope programmes", value="ALL")
            scope_batches = st.text_input("Scope batches", value="ALL")
            retrieved_on = st.date_input("Retrieved on *", value=date.today())
            synthetic = st.radio("Synthetic document?", ["N", "Y"], horizontal=True)
        submitted = st.form_submit_button("Ingest")
    if not submitted:
        return
    required = {"doc_id": doc_id, "title": title, "issuer": issuer, "version": version, "provenance": provenance}
    missing = [k for k, v in required.items() if not v.strip()]
    if upload is None:
        missing.insert(0, "file")
    if missing:
        st.error("Missing required fields: " + ", ".join(missing))
        return
    metadata = {
        "doc_id": doc_id.strip(), "title": title.strip(), "issuer": issuer.strip(),
        "authority_level": int(authority_level), "doc_type": doc_type, "version": version.strip(),
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if has_end else None,
        "supersedes": supersedes.strip(), "scope_programmes": scope_programmes.strip() or "ALL",
        "scope_batches": scope_batches.strip() or "ALL", "provenance": provenance.strip(),
        "retrieved_on": retrieved_on.isoformat(), "synthetic": synthetic,
    }
    with st.spinner("Uploading and indexing…"):
        result = client().ingest(upload.name, upload.getvalue(), metadata, upload.type or "application/octet-stream")
    if result.ok:
        st.success(
            f"Backend status: **{result.data.get('status')}** · doc_id `{result.data['doc_id']}` · "
            f"chunks indexed: **{result.data['chunks_indexed']}**"
        )
    else:
        st.error(result.error)


def main() -> None:
    init_state()
    st.title("🎓 University Student Services Assistant")
    st.caption(
        "Ask about university policies (answers cite their sources) or about your own attendance, "
        "results and eligibility (calculated by code from your records)."
    )
    student_id, as_of = render_sidebar()
    tab_ask, tab_sources, tab_audit, tab_ingest = st.tabs(["Ask", "Sources", "Audit", "Ingest document"])
    with tab_sources:
        sources_tab()
    with tab_audit:
        audit_tab()
    with tab_ingest:
        ingest_tab()
    with tab_ask:
        ask_tab(student_id, as_of)


main()
