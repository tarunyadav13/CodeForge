"""Minimal Streamlit screen for the shared foundation.

Talks to the FastAPI backend and renders the AskResponse contract. This is NOT the
full wireframe from the blueprint - it proves the UI <-> API wiring only.
"""
import os
from datetime import date

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000")
STUDENT_OPTIONS = ["(no student - general question)", "S1001", "S1002"]  # placeholders only

BADGES = {
    "retrieved_fact": "Retrieved fact",
    "calculated": "Calculated",
    "not_found": "Not found",
    "clarification_needed": "Clarification needed",
    "refused": "Refused",
    "conflict_flagged": "Conflict flagged",
}

st.set_page_config(page_title="University Student Services Assistant", layout="wide")
st.title("AI-Powered University Student Services Assistant")
st.caption("Grounded answers from authorised university sources. Foundation build: responses are MOCK.")

# ---- sidebar: identity + date + health
with st.sidebar:
    st.subheader("Session")
    choice = st.selectbox("Student (sent as X-Student-Id)", STUDENT_OPTIONS)
    student_id = None if choice == STUDENT_OPTIONS[0] else choice
    as_of = st.date_input("As-of date", value=date.today())
    st.subheader("System status")
    try:
        h = requests.get(f"{API_URL}/health", timeout=3).json()
        for name in ("api", "vector_store", "sqlite", "llm"):
            s = h[name]["status"]
            st.write(f"{'🟢' if s == 'ok' else '⚪'} {name}: {s}")
    except requests.RequestException:
        st.error("API unreachable. Start it with uvicorn (see README).")

# ---- suggested questions
suggestions = [
    "What is the minimum attendance required for exam eligibility?",
    "My attendance is 72%. Am I eligible for the exam?",
    "Am I eligible?",
]
cols = st.columns(len(suggestions))
for col, text in zip(cols, suggestions):
    if col.button(text, use_container_width=True):
        st.session_state["pending"] = text

if "history" not in st.session_state:
    st.session_state["history"] = []

typed = st.chat_input("Ask about attendance, eligibility, or university policy")
question = typed or st.session_state.pop("pending", None)

if question:
    headers = {"X-Student-Id": student_id} if student_id else {}
    try:
        with st.spinner("Searching authorised sources..."):
            r = requests.post(
                f"{API_URL}/ask",
                json={"question": question, "as_of_date": as_of.isoformat()},
                headers=headers,
                timeout=60,
            )
        r.raise_for_status()
        st.session_state["history"].append((question, r.json(), None))
    except requests.RequestException as exc:
        st.session_state["history"].append((question, None, str(exc)))

for q, data, err in st.session_state["history"]:
    with st.chat_message("user"):
        st.write(q)
    with st.chat_message("assistant"):
        if err:
            st.error(f"Request failed: {err}")
            continue
        st.markdown(f"**{BADGES.get(data['answer_type'], data['answer_type'])}**")
        st.write(data["answer"])
        if data.get("explanation"):
            st.caption(data["explanation"])
        if data["citations"]:
            with st.expander("Citations"):
                for c in data["citations"]:
                    st.write(
                        f"{c['doc_id']} - {c['title']} | section {c.get('section')} | "
                        f"page {c.get('page')} | v{c.get('version')} | effective {c.get('effective_from')}"
                    )
        if data["tools_invoked"]:
            with st.expander("Tool results"):
                st.json(data["tools_invoked"])
        if data["conflicts_detected"]:
            with st.expander("Conflicts"):
                st.json(data["conflicts_detected"])
        st.caption(f"trace_id: {data['trace_id']}")
