"""SBTi targets, base year, scenario pathways and MACC."""
from common import PALETTE, csv_bytes, evidence, page

import plotly.graph_objects as go
import streamlit as st

from juuri import pathways as P
from juuri.data import raw

policy = page("Targets, pathways & abatement", "🎯")
b = P.base_inventory(policy)

st.subheader("1 · Base year")
by = P.base_year_options(b)
st.dataframe(by[["option", "scope1", "scope2", "scope12", "scope3", "note"]].round(0), hide_index=True, width="stretch")
evidence("structural_events.csv: grain trade divested 2022, Foodhills acquired 27 Nov 2025 (Bjuv in scope from December; ~300 SE pea "
         "growers from 2026). SBTi V2 prefers the latest complete year → 2025, structurally adjusted.")

st.subheader("2 · SBTi near-term targets")
cc = st.columns(3)
ty = cc[0].slider("Target year", 2030, 2035, 2032)
scen_names = list(raw()["scenarios"].scenario.unique())
scenario = cc[1].selectbox("Transition scenario (NGFS Phase V, illustrative prices)", scen_names, index=scen_names.index("net_zero_2050"))
scenario2 = cc[2].selectbox("Compare with", scen_names, index=scen_names.index("current_policies"))
tg = P.targets(b, target_year=ty)
cols = st.columns(3)
names = {"scope12": "Scope 1+2", "scope3_nonflag": "Scope 3 non-FLAG", "flag": "FLAG"}
for i, (_, r) in enumerate(tg.iterrows()):
    cols[i].metric(f"{names[r.boundary]} target {ty}", f"{r.target_tco2e:,.0f} t", f"−{r.reduction_pct:.1f} % vs {r.base_tco2e:,.0f} t", delta_color="off")
    cols[i].caption(r.source)

st.subheader("3 · Pathway: BAU vs levers vs target")
lv = P.lever_table(b)
ab = lv[lv.kind == "abatement"]
default = list(ab[ab.confidence.isin(["high", "medium"])].lever_id)
chosen = st.multiselect("Levers in the portfolio", list(ab.lever_id), default=default,
                        format_func=lambda k: f"{k} · {ab.set_index('lever_id').loc[k, 'name']}")
boundary = st.radio("Boundary", list(names), format_func=names.get, horizontal=True)
fig = go.Figure()
for sc, dash in [(scenario, "solid"), (scenario2, "dot")]:
    pw = P.pathway(b, sc, chosen, end=2035, target_year=ty)
    fig.add_scatter(x=pw.year, y=pw[f"bau_{boundary}"], name=f"BAU · {sc}", line=dict(color=PALETTE["bau"], dash=dash))
    fig.add_scatter(x=pw.year, y=pw[f"net_{boundary}"], name=f"With levers · {sc}", line=dict(color=PALETTE["net"], dash=dash, width=3))
fig.add_scatter(x=pw.year, y=pw[f"target_{boundary}"], name="SBTi linear target", line=dict(color=PALETTE["target"], dash="dash"))
fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="t CO2e", legend=dict(orientation="h"))
st.plotly_chart(fig)
pw1 = P.pathway(b, scenario, chosen, end=2035, target_year=ty)
gap = pw1.set_index("year")[f"gap_{boundary}"]
st.markdown(f"Gap to target in {ty} under **{scenario}**: **{gap[ty]:+,.0f} t** "
            f"({'on track' if gap[ty] <= 0 else 'more action needed'}). Lever cost in {ty}: "
            f"€{pw1.set_index('year').lever_cost_eur[ty]:,.0f}/yr (net of avoided ETS2 fuel cost).")
evidence("BAU = 2025 adjusted base × plant-based demand index of the scenario; Pudasjärvi closure moves pizza from Scope 1/2 to "
         "cat 1 (structural, not abatement). Levers ramp from their earliest start. L05, L26 (structural) and L27 "
         "(measurement change) are excluded from abatement.")

st.subheader("4 · Marginal abatement cost curve (2030)")
m = P.macc(b, scenario)
m = m[m.lever_id.isin(chosen)]
m["x_end"] = m.abatement_tco2e.cumsum()
m["x_start"] = m.x_end - m.abatement_tco2e
col = {"scope12": PALETTE["scope1"], "scope3_nonflag": PALETTE["nonflag"], "flag": PALETTE["flag"]}
fig = go.Figure()
for _, r in m.iterrows():
    fig.add_bar(x=[(r.x_start + r.x_end) / 2], y=[r.cost_eur_per_t_scenario], width=[r.abatement_tco2e], marker_color=col[r.boundary],
                marker_line=dict(color="white", width=1), showlegend=False,
                hovertext=f"{r.lever_id} {r['name']}<br>{r.abatement_tco2e:,.0f} t (±{100 * r.uncertainty_frac:.0f} %)<br>"
                          f"€{r.cost_eur_per_t_scenario:,.0f}/t<br>{r.abatement_basis}", hoverinfo="text")
fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0), xaxis_title="cumulative abatement, t CO2e/yr",
                  yaxis_title="€ per t CO2e", bargap=0)
st.plotly_chart(fig)
st.caption("Colour: green = Scope 1+2, blue = Scope 3 non-FLAG, brown = FLAG. Hover for uncertainty and basis. "
           "Uncertainty band ±20/40/60 % for high/medium/low confidence.")
st.dataframe(lv[["lever_id", "name", "kind", "boundary", "abatement_tco2e", "abatement_basis", "cost_eur_per_t", "confidence",
                 "earliest_start", "owner", "co_benefits", "trade_offs"]].round(0), hide_index=True, width="stretch")
st.subheader("Scope 3 coverage check")
st.dataframe(P.scope3_coverage(b)[["category", "tco2e", "share_of_scope3", "must_be_covered_v2"]].round(3), hide_index=True)
evidence("SBTi V2: near-term Scope 3 targets must cover every category ≥ 5 % of Scope 3. Category 10 (processing of sold "
         "oil/meal) is not calculated: screen it before submission.")
st.download_button("Download pathway", csv_bytes(pw1), "pathway.csv")
