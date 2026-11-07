"""Live context: FIWARE Orion-LD (NGSI-LD, Smart Data Models)."""
from common import evidence, page

import json

import pandas as pd
import pydeck as pdk
import streamlit as st

from juuri import fiware as FW, review as RV
from juuri.climate import parcels as cparcels

page("Live context (FIWARE)", "📡")
b = FW.Broker()
if not b.alive():
    st.warning(f"No NGSI-LD context broker at `{FW.BROKER}`.")
    st.code("python -m juuri fiware up      # Orion-LD + MongoDB (Docker)\n"
            "python -m juuri fiware load    # publish entities, results and subscriptions\n"
            "python -m juuri fiware demo    # listener + live simulator", language="bash")
    st.stop()

stt = FW.status(b)
st.caption(f"Orion-LD {stt['version']} at `{FW.BROKER}` · @context `{stt['context']}` (Juuri terms + "
           f"[Smart Data Models]({FW.SDM_CONTEXT}))")
c = st.columns(len(stt["entities"]))
for i, (k, v) in enumerate(stt["entities"].items()):
    c[i].metric(k, f"{v:,}")
subs = pd.DataFrame(stt["subscriptions"])
if len(subs):
    st.dataframe(subs, hide_index=True, width="stretch")

st.subheader("Live data-quality issues")
st.caption("Raised by Juuri the moment a meter reading arrives (subscription → seasonal-norm check). Same record keys as the "
           "batch agent: a decision here is written to the broker and to `review/decisions.csv`.")
iss = pd.DataFrame(b.query("DataQualityIssue", limit=500))
if iss.empty:
    st.info("No live issues yet. Run `python -m juuri fiware demo` (or `listen` + `simulate`).")
else:
    cols = ["id", "issueType", "recordKey", "observedValue", "proposedValue", "confidence", "explanation", "reviewStatus", "detectedAt"]
    iss = iss[[c for c in cols if c in iss.columns]]
    view = iss.assign(select=False)
    ed = st.data_editor(view, hide_index=True, width="stretch", disabled=[c for c in view.columns if c != "select"])
    chosen = ed[ed.select]
    a1, a2, a3 = st.columns([1, 1, 2])
    reviewer = a3.text_input("Reviewer", value=st.session_state.get("reviewer", ""))
    for col, label, decision in [(a1, "✅ Approve", "approved"), (a2, "❌ Reject", "rejected")]:
        if col.button(label, disabled=chosen.empty or not reviewer):
            for _, r in chosen.iterrows():
                b.patch(r.id, {"reviewStatus": FW.prop(decision, source=f"reviewer {reviewer}")})
                RV.save_decision(r.recordKey, "value", decision, reviewer, "live review (FIWARE)")
            st.session_state["reviewer"] = reviewer
            st.rerun()

st.subheader("Drought impact on contract parcels (season to date)")
par = pd.DataFrame(b.query("AgriParcel", attrs="expectedYieldImpact,droughtAlert,waterDeficitToDate,area,hasAgriCrop,belongsTo", limit=5000))
if "expectedYieldImpact" not in par.columns:
    st.info("No live weather processed yet.")
else:
    par["crop"] = par.hasAgriCrop.str.split(":").str[-1]
    par["parcel_id"] = par.id.str.split(":").str[-1]
    cp = cparcels().set_index("parcel_id")
    par["lat"], par["lon"], par["region"] = par.parcel_id.map(cp.lat), par.parcel_id.map(cp.lon), par.parcel_id.map(cp.region)
    g = par.groupby(["region", "crop"]).apply(lambda x: pd.Series(dict(
        parcels=len(x), area_ha=x.area.sum(), impact_pct=(x.expectedYieldImpact * x.area).sum() / x.area.sum(),
        moderate=(x.droughtAlert == "moderate").sum(), high=(x.droughtAlert == "high").sum(),
        water_deficit_mm=x.waterDeficitToDate.mean())), include_groups=False).reset_index()
    st.dataframe(g.round(1), hide_index=True, width="stretch")
    region = st.radio("Map region", sorted(par.region.dropna().unique()), horizontal=True)
    m = par[par.region == region].dropna(subset=["lat"]).copy()
    m["color"] = m.expectedYieldImpact.apply(lambda v: [178, 58, 72, 200] if v <= -15 else [201, 162, 39, 200] if v <= -7
                                             else [47, 111, 79, 160] if v >= 0 else [154, 165, 173, 160])
    st.pydeck_chart(pdk.Deck(layers=[pdk.Layer("ScatterplotLayer", m, get_position=["lon", "lat"], get_radius=250,
                                               get_fill_color="color", pickable=True)],
                             initial_view_state=pdk.ViewState(latitude=float(m.lat.mean()), longitude=float(m.lon.mean()), zoom=8.5),
                             map_style=None, tooltip={"html": "{parcel_id} · {crop}<br/>impact {expectedYieldImpact} % · {droughtAlert}"}))
    evidence("AgriParcel.expectedYieldImpact = Juuri yield model with season-to-date weather from the parcel's WeatherObserved cell "
             "vs 1991–2020 climatology (rest of season at climatology). Red ≤ −15 %, amber ≤ −7 %, green ≥ 0.")

st.subheader("Weather stations (latest observation)")
wo = pd.DataFrame(b.query("WeatherObserved", attrs="dateObserved,temperature,maxTemperature,precipitation,potentialEvapotranspiration", limit=200))
st.dataframe(wo, hide_index=True, width="stretch", height=220)
if st.button("▶ Advance live simulation by 7 days", help="Needs `python -m juuri fiware listen` running to react to the updates."):
    last = pd.to_datetime(wo.dateObserved, errors="coerce").max() if "dateObserved" in wo else pd.NaT
    start = (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if pd.notna(last) else "2026-05-01"
    with st.spinner(f"Streaming 7 days from {start}…"):
        FW.simulate(days=7, start=start, meters=False)
    st.rerun()

st.subheader("NGSI-LD query console")
q1, q2, q3 = st.columns([1, 2, 1])
etype = q1.selectbox("type", ["AgriParcel", "AgriFarm", "Catchment", "Building", "WeatherObserved", "DeviceMeasurement", "DataQualityIssue", "AgriCrop"])
query = q2.text_input("q (NGSI-LD query language)", value='droughtAlert=="moderate"' if etype == "AgriParcel" else "")
limit = q3.number_input("limit", 1, 1000, 20)
try:
    res = b.query(etype, q=query or None, limit=int(limit))
    st.caption(f"GET {FW.NGSI}/entities?type={etype}" + (f"&q={query}" if query else "") + f"&limit={limit}&options=keyValues · {len(res)} entities")
    st.json(json.loads(json.dumps(res[:limit], default=str)), expanded=False)
except Exception as e:  # noqa: BLE001
    st.error(str(e))

ev = FW.read_events(300)
if len(ev):
    st.subheader("Juuri reactions (event log)")
    st.dataframe(ev.iloc[::-1], hide_index=True, width="stretch", height=240)
