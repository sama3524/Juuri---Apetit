"""Copilot with evidence."""
from common import csv_bytes, page

import os

import pandas as pd
import streamlit as st

from juuri import config as C, copilot as CP

page("Copilot", "💬")


def _has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


live = _has_credentials()
st.caption(("Claude (" + CP.MODEL + ") answers using Juuri's own tools only and cites the records and documents behind each number."
            if live else "Offline mode: set ANTHROPIC_API_KEY to enable the Claude agent. Until then, questions are matched to "
            "Juuri's computed answers and the framework documents."))

if "chat" not in st.session_state:
    st.session_state.chat = []
    st.session_state.agent = None

examples = ["What share of our 2025 emissions is FLAG, and do we need a FLAG target?",
            "Which data corrections changed Scope 1 at Säkylä in 2025?",
            "What is the cheapest way to close the Scope 1+2 gap by 2032?",
            "Which catchment should the farming advisors visit first, and why?"]
cols = st.columns(len(examples))
clicked = None
for i, e in enumerate(examples):
    if cols[i].button(e, width="stretch"):
        clicked = e

for m in st.session_state.chat:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("trace"):
            with st.expander("Evidence trail"):
                st.json(m["trace"])

q = st.chat_input("Ask about emissions, targets, climate risk, nature or the data…") or clicked
if q:
    st.session_state.chat.append(dict(role="user", content=q))
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        with st.spinner("Looking it up…"):
            try:
                if live:
                    if st.session_state.agent is None:
                        st.session_state.agent = CP.Copilot()
                    text, trace = st.session_state.agent.ask(q)
                else:
                    text, trace = CP.offline_answer(q)
            except Exception as e:  # noqa: BLE001
                text, trace = f"Copilot error: {e}. Falling back to offline answer.\n\n" + CP.offline_answer(q)[0], []
        st.markdown(text)
        if trace:
            with st.expander("Evidence trail"):
                st.json(trace)
    st.session_state.chat.append(dict(role="assistant", content=text, trace=trace))

st.divider()
st.subheader("Challenge questions (E) with evidence")
qs = pd.read_csv(C.TASKS / "E_questions.csv")
a = CP.answer_e().merge(qs[["question_id", "question", "unit"]], on="question_id", how="left")
st.dataframe(a[["question_id", "question", "answer", "unit", "evidence"]], hide_index=True, width="stretch", height=420)
st.download_button("Download E submission", csv_bytes(CP.e_submission()), "E_answers_submission.csv")
