"""Method note, validation and production path."""
from common import ROOT, page

import json

import streamlit as st

from juuri import config as C

page("Method, validation & sources", "📚")
scores = C.OUTPUTS / "dev_scores.json"
if scores.exists():
    st.subheader("Validation on the public dev keys")
    st.json(json.loads(scores.read_text()))
else:
    st.info("Run `python -m juuri score` to compute dev-key scores.")
st.markdown((ROOT / "docs" / "METHOD_NOTE.md").read_text())
