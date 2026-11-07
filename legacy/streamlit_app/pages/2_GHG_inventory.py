"""GHG inventory with drill-down to source records."""
from common import PALETTE, SCOPE_LABEL, T, csv_bytes, evidence, page, status_badge

import pandas as pd
import plotly.express as px
import streamlit as st

from juuri import inventory as INV
from juuri.data import raw

policy = page("GHG inventory & audit trail", "📊")
t = T(policy)
st.markdown(status_badge(t), unsafe_allow_html=True)
year = st.select_slider("Reporting year", options=[2021, 2022, 2023, 2024, 2025], value=2025)
inv = INV.line_items(t, year)
s = INV.summary(t, year).set_index("category").tco2e

c = st.columns(5)
c[0].metric("Scope 1", f"{s['scope1_total']:,.0f} t")
c[1].metric("Scope 2 location", f"{s['scope2_location_total']:,.0f} t")
c[2].metric("Scope 2 market", f"{s['scope2_market_total']:,.0f} t")
c[3].metric("Scope 3", f"{s['scope3_total']:,.0f} t")
c[4].metric("Biogenic CO2 (memo)", f"{s['biogenic_co2_scope1']:,.0f} t", help="Reported separately, not in Scope 1")

l, r = st.columns([3, 2])
with l:
    cat = inv[inv.scope != "scope2_location"].groupby(["scope", "category"]).tco2e.sum().reset_index()
    cat["label"] = cat.category.map(INV.CATEGORY_LABEL).fillna(cat.category)
    cat["scope_label"] = cat.scope.map(SCOPE_LABEL)
    fig = px.bar(cat.sort_values("tco2e"), x="tco2e", y="label", color="scope_label", orientation="h", height=430,
                 color_discrete_map={"Scope 1": PALETTE["scope1"], "Scope 2 (market)": PALETTE["scope2"], "Scope 3": PALETTE["scope3"]},
                 log_x=st.toggle("Log scale", value=True))
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_title=None, xaxis_title="t CO2e", legend_title=None)
    st.plotly_chart(fig)
with r:
    st.markdown("**FLAG vs non-FLAG (Scope 3 cat 1)**")
    fl = INV.flag_split(t, year)
    f = pd.DataFrame(dict(part=["FLAG (land-based)", "non-FLAG"], t=[fl.flag_tco2e.sum(), fl[fl.scope == "scope3"].non_flag_tco2e.sum()]))
    fig = px.pie(f, names="part", values="t", hole=0.55, color="part",
                 color_discrete_map={"FLAG (land-based)": PALETTE["flag"], "non-FLAG": PALETTE["nonflag"]}, height=300)
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), legend=dict(orientation="h"))
    st.plotly_chart(fig)
    evidence("FLAG = factor flag_share × purchased commodities + contract-farm N2O, liming and drained organic soils "
             "(METHODOLOGY §5). Fertiliser manufacture, diesel and electricity are non-FLAG.")

st.subheader("Drill down: line item → source records")
cats = sorted(inv.category.unique(), key=lambda x: INV.CATEGORY_LABEL.get(x, x))
cc = st.columns([1, 2])
category = cc[0].selectbox("Category", cats, format_func=lambda x: INV.CATEGORY_LABEL.get(x, x),
                           index=cats.index("cat01_purchased_goods") if "cat01_purchased_goods" in cats else 0)
items = inv[inv.category == category].sort_values("tco2e", ascending=False)
item = cc[1].selectbox("Line item", items.line_item + "  ·  " + items.tco2e.map("{:,.1f} t".format) + "  ·  " + items.scope)
item_id, _, item_scope = [p.strip() for p in item.split("·")]
row = items[(items.line_item == item_id) & (items.scope == item_scope)].iloc[0]
st.markdown(f"**{item_id}** · {row.tco2e:,.1f} t CO2e · method: *{row.basis}*")
lin = INV.lineage(t, year)
lin = lin[(lin.line_item == item_id) & (lin.scope == item_scope)]
if len(lin):
    audit = t["_audit"]
    keys = set(lin.record_key.str.split(" <- ").str[-1].str.split(";").explode()) | set(lin.record_key)
    touched = audit[audit.record_key.isin(keys)]
    lin = lin.assign(corrected=lin.record_key.apply(lambda k: any(a in k for a in touched.record_key)) if len(touched) else False)
    st.dataframe(lin.sort_values("tco2e", ascending=False), hide_index=True, width="stretch", height=280,
                 column_config={"tco2e": st.column_config.NumberColumn(format="%.3f")})
    st.caption(f"{len(lin)} source records sum to {lin.tco2e.sum():,.1f} t.")
    if len(touched):
        st.markdown(f"**{len(touched)} corrections affect these records**")
        st.dataframe(touched[["record_key", "field", "old_value", "new_value", "issue_type", "rule_or_model", "status", "approved_by"]],
                     hide_index=True, width="stretch")
else:
    st.info("Record-level lineage for this category is computed from aggregated activity (e.g. survey × headcount, sales × "
            "product-group factors); the method and factors are listed in the basis column.")

st.subheader("Organic soils: what today's calculator misses")
cur = raw()["current_calc"]
fd = t["farm_detail"]
fd = fd[(fd.year == year) & (fd.region == "FI_SW")]
m = fd.merge(cur, on=["farm_id", "crop", "year"], how="left")
m["current_t"] = m.carbon_footprint_kgco2e_per_kg * m.delivered_kg / 1000
g = m.groupby("crop").agg(juuri_t=("tco2e", "sum"), current_calculator_t=("current_t", "sum"), organic_soil_t=("organic_tco2e", "sum"),
                         delivered_t=("delivered_kg", lambda x: x.sum() / 1000)).reset_index()
g["juuri_kg_per_kg"] = g.juuri_t / g.delivered_t
st.dataframe(g.round(2), hide_index=True, width="stretch")
evidence("current_calculator_footprints.csv assumes mineral soil (respondents only). Juuri uses the soil map + survey per farm, "
         "includes drained organic soils (29 t CO2/ha/yr) and imputes non-respondents from their own history.")

st.download_button("Download line items", csv_bytes(inv), f"inventory_line_items_{year}.csv")
st.download_button("Download A2 submission (2025)", csv_bytes(INV.a2_submission(t)), "A2_inventory_2025_submission.csv")
