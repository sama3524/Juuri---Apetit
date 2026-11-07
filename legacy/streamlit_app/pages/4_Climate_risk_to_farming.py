"""Climate risk to contract farming."""
from common import csv_bytes, evidence, page

import plotly.graph_objects as go
import streamlit as st

from juuri import climate as CL

page("Climate risk to farming", "🌦️")
st.caption("Yield model learnt from 2012–2026 farm yields and daily weather; applied to the 2025 parcel mix with today's "
           "irrigation and management under three climate futures.")

with st.spinner("Running the yield model over 1991–2060 weather…"):
    cy = CL.climate_yields()
    risk = CL.c2_risk()
    eur = CL.c3_euro_risk()

c = st.columns(2)
crop = c[0].selectbox("Crop", ["turnip_rape", "pea", "potato", "carrot", "spinach"])
regions = sorted(cy[cy.crop == crop].region.unique())
region = c[1].selectbox("Region", regions)
x = cy[(cy.crop == crop) & (cy.region == region)]
fig = go.Figure()
colors = {"historical": "#555", "ssp126": "#2f6f4f", "ssp245": "#c9a227", "ssp585": "#b23a48"}
for sc, g in x.groupby("scenario"):
    g = g.sort_values("year")
    fig.add_scatter(x=g.year, y=g.yield_t_ha, name=sc, mode="lines", line=dict(color=colors[sc], width=1), opacity=0.45)
    fig.add_scatter(x=g.year, y=g.yield_t_ha.rolling(9, center=True, min_periods=4).mean(), name=f"{sc} (9-yr mean)",
                    line=dict(color=colors[sc], width=3))
base = x[(x.scenario == "historical") & x.year.between(1991, 2020)].yield_t_ha.mean()
fig.add_hline(y=0.8 * base, line_dash="dash", line_color="#b23a48", annotation_text="bad year (< 80 % of 1991–2020)")
fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="t/ha (area-weighted)", legend=dict(orientation="h"))
st.plotly_chart(fig)

l, r = st.columns(2)
with l:
    st.markdown("**2041–2060 vs 1991–2020**")
    st.dataframe(risk[["region", "crop", "scenario", "mean_yield_change_pct", "p_bad_year", "p_bad_year_1991_2020"]],
                 hide_index=True, width="stretch", height=360)
with r:
    st.markdown("**€ exposure: shortfall × contract price × (1 + import premium)**")
    e = eur.copy()
    e["expected_cost_k€"] = e.expected_cost_eur / 1000
    e["p95_cost_k€"] = e.p95_cost_eur / 1000
    st.dataframe(e[["region", "crop", "scenario", "expected_shortfall_t", "expected_cost_k€", "p95_cost_k€", "replacement_tco2e"]].round(0),
                 hide_index=True, width="stretch", height=360)
evidence("Prices: contract_prices_and_replacement_premiums.csv (latest year). Plan volume = 1991–2020 mean production on the 2025 mix. "
         "Replacement imports add emissions (cat 1), shown as replacement_tco2e.")

st.subheader("Adaptation: value of irrigation (2041–2060)")
cc = st.columns(3)
ac = cc[0].selectbox("Crop ", ["potato", "carrot", "pea", "spinach"])
asc = cc[1].selectbox("Scenario", ["ssp585", "ssp245", "ssp126"])
ad = CL.adaptation_irrigation(ac, "FI_SW", asc)
cc[2].metric("Yield gain if all parcels irrigated", f"{ad['gain_pct']:+.1f} %", f"{ad['yield_gain_t_per_yr']:,.0f} t/yr")
evidence("Learnt from the drought × irrigation interaction in survey yields. Pair with lever L24 (irrigation scheduling & drip) "
         "and the water-availability screening on the Nature page.")

st.subheader("Two-region hedge (peas)")
p = cy[(cy.crop == "pea") & (cy.scenario == "historical")].pivot_table(index="year", columns="region", values="prod")
if p.shape[1] == 2:
    corr = p.pct_change().corr().iloc[0, 1]
    cv_sep = (p.std() / p.mean()).round(3).to_dict()
    tot = p.sum(axis=1)
    st.markdown(f"Year-to-year correlation of Finnish and Swedish pea production: **{corr:.2f}**. Coefficient of variation: "
                f"FI {cv_sep.get('FI_SW', 0):.1%}, SE {cv_sep.get('SE_SKANE', 0):.1%}, combined **{tot.std() / tot.mean():.1%}**. "
                "Sourcing from both regions lowers volume risk (2026: record FI pea year, drought-hit SE).")

st.subheader("Model transparency")
bt = CL.backtest(2019)
st.markdown("**Backtest: train ≤ 2019, predict 2020–2026** (lower is better; climatology = farm's historical mean)")
st.dataframe(bt.round(3), hide_index=True, width="stretch")
crop_c = st.selectbox("Show coefficients for", list(CL.model().models))
coef = CL.model().coefficients(crop_c)
fig = go.Figure(go.Bar(x=coef.values, y=coef.index, orientation="h", marker_color=["#b23a48" if v < 0 else "#2f6f4f" for v in coef.values]))
fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0), xaxis_title="standardised effect on log yield")
st.plotly_chart(fig)
st.download_button("Download C1 submission", csv_bytes(CL.c1_predictions()), "C1_yields_2027_2031_submission.csv")
st.download_button("Download C2 submission", csv_bytes(CL.c2_submission()), "C2_climate_yield_risk_submission.csv")
