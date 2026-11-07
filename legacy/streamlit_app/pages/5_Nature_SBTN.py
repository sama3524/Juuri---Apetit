"""Nature: catchment prioritisation (SBTN step 2) and value-chain screening (step 1)."""
from common import csv_bytes, evidence, page

import copy

import numpy as np
import pydeck as pdk
import streamlit as st

from juuri import nature as N

page("Nature impacts (SBTN)", "💧")
st.caption("Pressure × state of nature: how much phosphorus and nitrogen Apetit's contract fields deliver to each receiving "
           "water, weighted by its ecological status and sensitivity.")

pr = N.d1_priority()
loads = N.d1_loads()
cal = N.calibration()

c = st.columns(4)
c[0].metric("Apetit P to water 2025", f"{pr.apetit_p_to_water_kg.sum():,.0f} kg")
c[1].metric("Apetit N to water 2025", f"{pr.apetit_n_to_water_kg.sum():,.0f} kg")
c[2].metric("Priority catchments in poor/bad status", int(((pr.priority_rank <= 10) & pr.ecological_status_class.isin(["poor", "bad"])).sum()), help="of the top 10")
c[3].metric("Model check (dev catchments)", f"±{10 ** cal['p']['dev_mae_log10'] * 100 - 100:.0f} % P", help="Mean absolute error in log space on the 6 public dev catchments (in-sample)")

region = st.radio("Region", ["FI_SW", "SE_SKANE"], horizontal=True)
gj = copy.deepcopy(N.catchments_geojson())
pi = pr.set_index("catchment_id")
mx = pi.priority_score.max()
for f in gj["features"]:
    cid = f["properties"]["catchment_id"]
    sc = float(pi.priority_score.get(cid, 0.0))
    f["properties"].update(dict(priority_rank=int(pi.priority_rank.get(cid, 0)), priority_score=round(sc, 2),
                                p_kg=round(float(pi.apetit_p_to_water_kg.get(cid, 0)), 1),
                                fill=[178, 58, 72, int(30 + 190 * sc / mx)] if sc > 0 else [200, 200, 200, 40]))
gj["features"] = [f for f in gj["features"] if f["properties"]["region"] == region]
pts = loads[loads.catchment_id.str.startswith("CA" if region == "FI_SW" else "CS")].copy()
pts["p_ha"] = pts.p_kg / pts.area_ha
pts["r"] = 60 + 400 * np.sqrt(pts.p_kg / pts.p_kg.max())
view = pdk.ViewState(latitude=float(pts.lat.mean()), longitude=float(pts.lon.mean()), zoom=8.3)
deck = pdk.Deck(
    layers=[pdk.Layer("GeoJsonLayer", gj, get_fill_color="properties.fill", get_line_color=[80, 80, 80], line_width_min_pixels=1, pickable=True),
            pdk.Layer("ScatterplotLayer", pts, get_position=["lon", "lat"], get_radius="r", get_fill_color=[47, 111, 79, 170], pickable=True)],
    initial_view_state=view, map_style=None,
    tooltip={"html": "<b>{catchment_id}</b> {receiving_water_type} · {ecological_status_class}<br/>rank {priority_rank} · P {p_kg} kg"
                     "<br/>{parcel_id} {crop} {area_ha} ha"})
st.pydeck_chart(deck)
st.caption("Red shading = catchment priority; green dots = contract parcels sized by P delivered. Geometry is synthetic; the same "
           "pipeline runs on SYKE catchments and Ruokavirasto parcels.")

st.subheader("Catchment priority (D1)")
st.dataframe(pr[["priority_rank", "catchment_id", "region", "receiving_water_type", "ecological_status_class", "apetit_area_ha",
                 "apetit_p_to_water_kg", "apetit_n_to_water_kg", "state_weight", "p_sensitivity", "priority_score",
                 "apetit_share_of_monitored_p"]].round(3), hide_index=True, width="stretch", height=320)
evidence("priority = (P/median(P) + 0.5·N/median(N)) × state_weight × p_sensitivity (NATURE_SBTN.md). Loads: export coefficients "
         f"(1.1 kg P, 15 kg N per ha) × soil-P, slope, clay, crop, rain, buffer strip, catch crop, drainage, N surplus × "
         f"exp(−distance/{cal['p']['delivery_length_m']:.0f} m) × (1 − retention); scale calibrated on dev catchments.")

st.subheader("Field-level actions for farming advisors")
st.dataframe(N.parcel_hotspots(30).round(2), hide_index=True, width="stretch", height=300)

st.subheader("Value-chain materiality screening (D2, SBTN step 1)")
d2 = N.d2_screening()
st.dataframe(d2[["material", "origin_country", "t", "tco2e", "materiality_score", "why", "sbtn_high_impact", "eudr_commodity",
                 "water_stress_cat", "deforestation_risk", "biodiversity_sensitivity", "pressure_land_use", "pressure_water_use",
                 "pressure_nutrient_pollution", "pressure_pesticides"]].round(1), hide_index=True, width="stretch", height=360)
evidence("Volumes and emissions from the reviewed 2025 inventory; state of nature from origin_state_of_nature_indicative.csv "
         "(indicative; use WRI Aqueduct 4.0 and GFW for production). Pressures: ENCORE-style categories by material group.")
st.download_button("Download D1 submission", csv_bytes(N.d1_submission()), "D1_catchment_priority_submission.csv")
st.download_button("Download D2 screening", csv_bytes(d2), "D2_materiality_screening.csv")
