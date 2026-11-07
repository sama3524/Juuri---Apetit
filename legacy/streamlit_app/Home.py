"""Juuri - overview."""
from common import PALETTE, T, evidence, page, status_badge

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from juuri import inventory as INV, pathways as P
from juuri.data import raw

policy = page("Climate & Nature Navigator", "🌱")
t = T(policy)
st.markdown(status_badge(t), unsafe_allow_html=True)
st.caption("From fragmented raw feeds to traceable decisions. Every number below links back to source records, "
           "factors and reviewed corrections. Open the pages on the left to drill down.")

s25 = INV.summary(t, 2025).set_index("category").tco2e
fl = INV.flag_split(t, 2025)
tot = s25["scope1_total"] + s25["scope2_market_total"] + s25["scope3_total"]
c = st.columns(5)
c[0].metric("Scope 1 · 2025", f"{s25['scope1_total']:,.0f} t")
c[1].metric("Scope 2 market · 2025", f"{s25['scope2_market_total']:,.0f} t")
c[2].metric("Scope 3 · 2025", f"{s25['scope3_total']:,.0f} t")
c[3].metric("FLAG share of total", f"{100 * fl.flag_tco2e.sum() / tot:.0f} %", help="Land-based emissions: SBTi FLAG target mandatory")
fd = t["farm_detail"]
fd = fd[(fd.year == 2025) & (fd.region == "FI_SW")]
c[4].metric("Contract-farm emissions from peat soils", f"{100 * fd.organic_tco2e.sum() / fd.tco2e.sum():.0f} %",
            help="Drained organic soils on ~6 % of the area. Today's calculator assumes mineral soil and misses these.")

left, right = st.columns([3, 2])
with left:
    st.subheader("Inventory 2021–2025 vs published figures")
    rows = []
    for y in range(2021, 2026):
        s = INV.summary(t, y).set_index("category").tco2e
        rows.append(dict(year=y, scope1=s["scope1_total"], scope2=s["scope2_market_total"], scope3=s["scope3_total"]))
    df = pd.DataFrame(rows)
    pub = raw()["published"]
    fig = go.Figure()
    for k in ["scope3", "scope2", "scope1"]:
        fig.add_bar(x=df.year, y=df[k], name=k.replace("scope", "Scope "), marker_color=PALETTE[k])
    fig.add_scatter(x=pub.year, y=pub.scope1 + pub.scope2_market + pub.scope3_restated.fillna(pub.scope3), mode="markers+lines",
                    name="Published total (restated where available)", line=dict(color="#333", dash="dot"))
    fig.update_layout(barmode="stack", height=360, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="t CO2e", legend=dict(orientation="h"))
    st.plotly_chart(fig)
    evidence("Juuri recomputes every year from raw ERP, meter, fuel, refrigerant, survey and logistics feeds after data-quality "
             "correction. Published figures include the divested grain trade until 2022 and older methods.")

with right:
    st.subheader("What Apetit could act on next")
    b = P.base_inventory(policy)
    m = P.macc(b)
    top = m[m.abatement_tco2e > 500].sort_values("cost_eur_per_t_scenario").head(4)
    for _, l in top.iterrows():
        st.markdown(f"**{l['name']}** — {l.abatement_tco2e:,.0f} t/yr at ~€{l.cost_eur_per_t_scenario:,.0f}/t  \n"
                    f"<span class='ev'>Owner: {l.owner} · confidence {l.confidence} · {l.abatement_basis}</span>", unsafe_allow_html=True)
    st.markdown("**Fix the farm-data blind spot** — confirm soil type with the growers flagged for peat soils on the Data quality page; "
                "they drive most contract-farm emissions.  \n<span class='ev'>Owner: Farming services</span>", unsafe_allow_html=True)
    st.markdown("**Hedge turnip rape and carrot against drought** — the largest € climate exposure on the Climate risk page.  \n"
                "<span class='ev'>Owner: Sourcing + farming services</span>", unsafe_allow_html=True)

st.divider()
cols = st.columns(4)
iss = t["_issues"]
cols[0].metric("Data-quality issues found", f"{len(iss):,}")
cols[1].metric("Corrections applied", f"{int(iss.applied.sum()):,}")
cols[2].metric("Approved by a reviewer", f"{int((iss.status == 'approved').sum()):,}")
cols[3].metric("Rejected", f"{int((iss.status == 'rejected').sum()):,}")
