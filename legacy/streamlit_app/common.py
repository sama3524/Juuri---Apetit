"""Shared helpers for the Juuri Streamlit app."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from juuri import config as C  # noqa: E402

PALETTE = {"scope1": "#2f6f4f", "scope2": "#7aa95c", "scope3": "#c9a227", "flag": "#8c5a2b", "nonflag": "#5b7f95",
           "target": "#b23a48", "bau": "#9aa5ad", "net": "#2f6f4f"}
SCOPE_LABEL = {"scope1": "Scope 1", "scope2_location": "Scope 2 (location)", "scope2_market": "Scope 2 (market)", "scope3": "Scope 3"}


def page(title: str, icon: str = "🌱"):
    st.set_page_config(page_title=f"Juuri · {title}", page_icon=icon, layout="wide")
    st.markdown("""<style>
      .block-container {padding-top: 1.6rem;}
      div[data-testid="stMetricValue"] {font-size: 1.6rem;}
      .juuri-badge {display:inline-block;padding:2px 10px;border-radius:12px;font-size:0.8rem;font-weight:600;}
      .draft {background:#fff3cd;color:#7a5b00;border:1px solid #e8d18a;}
      .approved {background:#d8f0dd;color:#1f5c2e;border:1px solid #9fd3ab;}
      .ev {font-size:0.82rem;color:#5b6770;}
    </style>""", unsafe_allow_html=True)
    if not C.DATA.exists():
        st.error(f"Data package not found at `{C.PACKAGE_ROOT}`. Run `python -m juuri setup` first.")
        st.stop()
    with st.sidebar:
        st.markdown("### 🌱 Juuri")
        st.caption("Climate & Nature Navigator · synthetic twin of Apetit's data (Since AI 2026). "
                   "Not Apetit's actual emissions.")
        policy = st.radio("Corrections applied", ["provisional", "strict"], index=0,
                          help="provisional: apply reviewed + high-confidence proposed corrections (results marked DRAFT). "
                               "strict: apply only corrections a reviewer has approved.")
        st.session_state["policy"] = policy
    st.title(title)
    return st.session_state.get("policy", "provisional")


def T(policy: str):
    """Silver tables (cached in-process; rebuilt when review/decisions.csv changes)."""
    from juuri import inventory as INV
    with st.spinner("Building silver layer from raw feeds…"):
        return INV.canonical(policy)


def status_badge(T_) -> str:
    iss = T_["_issues"]
    applied = iss[iss.applied]
    prov = (applied.applied_as == "provisional").sum()
    if prov:
        return f'<span class="juuri-badge draft">DRAFT · {prov} corrections awaiting expert approval</span>'
    return '<span class="juuri-badge approved">Reviewed · all applied corrections approved</span>'


def fmt_t(x: float) -> str:
    return f"{x:,.0f} t" if abs(x) >= 10 else f"{x:,.1f} t"


def evidence(text: str):
    st.markdown(f'<div class="ev">📎 {text}</div>', unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()
