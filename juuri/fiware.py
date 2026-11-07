"""FIWARE live context layer: NGSI-LD + Smart Data Models + Orion-LD.

Juuri's batch pipeline answers "what happened and what should we do". The context broker makes it
live: farms, parcels, plants, weather cells, meters and catchments are NGSI-LD entities
(Smart Data Models where one exists), new observations stream into the broker, and Juuri reacts to
subscription notifications:

* meter reading  -> live data-quality check against the seasonal norm -> ``DataQualityIssue``
  entity awaiting expert review (same record key as the batch agent, so one review decision
  applies to both);
* daily weather   -> season-to-date water balance per grid cell -> the yield model re-estimates the
  drought impact on every contract parcel in that cell -> ``AgriParcel.droughtAlert``.

Results written to the broker carry sub-properties ``source`` and ``reviewStatus`` so traceability
survives outside Juuri. The broker is optional: ``python -m juuri run`` never needs it.

    python -m juuri fiware up         # docker compose: Orion-LD + MongoDB
    python -m juuri fiware load       # publish entities + results, create subscriptions
    python -m juuri fiware listen     # notification endpoint (port 8765)
    python -m juuri fiware simulate   # stream 2026 weather + replay the 2025 meter feed
    python -m juuri fiware demo       # listen + simulate together
    python -m juuri fiware status
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import numpy as np
import pandas as pd

from . import config as C

BROKER = os.environ.get("JUURI_BROKER", "http://localhost:1026").rstrip("/")
NGSI = BROKER + "/ngsi-ld/v1"
LISTEN_PORT = int(os.environ.get("JUURI_LISTEN_PORT", "8765"))
NOTIFY_URI = os.environ.get("JUURI_NOTIFY_URI", f"http://host.docker.internal:{LISTEN_PORT}/notify")
SDM_CONTEXT = "https://smartdatamodels.org/context.jsonld"
CORE_CONTEXT = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.9.jsonld"
NS = "https://github.com/juuri-navigator/ns#"
SDM_TERM = "https://smart-data-models.github.io/data-models/terms.jsonld#/definitions/"
CONTEXT_VERSION = "juuri:context-v3"
LIVE_DIR = C.OUTPUTS / "live"
COMPOSE = C.REPO_ROOT / "fiware" / "docker-compose.yml"

# Juuri extension terms (everything else comes from the Smart Data Models context)
JUURI_TERMS = {
    "juuri": NS, "reviewStatus": "juuri:reviewStatus", "source": "juuri:source", "sourceRecords": "juuri:sourceRecords",
    "method": "juuri:method", "Catchment": "juuri:Catchment", "DataQualityIssue": "juuri:DataQualityIssue",
    "issueType": "juuri:issueType", "recordKey": "juuri:recordKey", "observedValue": "juuri:observedValue",
    "proposedValue": "juuri:proposedValue", "rule": "juuri:rule", "explanation": "juuri:explanation",
    "confidence": "juuri:confidence", "refMeasurement": "juuri:refMeasurement",
    "refCatchment": "juuri:refCatchment", "refWeatherCell": "juuri:refWeatherCell",
    "ghgScope1": "juuri:ghgScope1", "ghgScope2Market": "juuri:ghgScope2Market", "farmEmissions": "juuri:farmEmissions",
    "flagEmissions": "juuri:flagEmissions", "organicSoilShare": "juuri:organicSoilShare", "footprintPerKg": "juuri:footprintPerKg",
    "phosphorusToWater": "juuri:phosphorusToWater", "nitrogenToWater": "juuri:nitrogenToWater", "priorityRank": "juuri:priorityRank",
    "priorityScore": "juuri:priorityScore", "ecologicalStatus": "juuri:ecologicalStatus", "receivingWaterType": "juuri:receivingWaterType",
    "pSensitivity": "juuri:pSensitivity", "waterDeficitToDate": "juuri:waterDeficitToDate", "droughtAlert": "juuri:droughtAlert",
    "expectedYieldImpact": "juuri:expectedYieldImpact", "yieldChange2041_2060": "juuri:yieldChange2041_2060",
    "badYearProbability2041_2060": "juuri:badYearProbability2041_2060", "soilClass": "juuri:soilClass",
    "potentialEvapotranspiration": "juuri:potentialEvapotranspiration", "detectedAt": "juuri:detectedAt",
    "climateScenario": "juuri:climateScenario",
    # Smart Data Models terms re-declared with the same IRIs but without "@type": "@id" coercion,
    # which Orion-LD rejects for NGSI-LD Relationships; and DeviceMeasurement/WeatherObserved terms
    # missing from the unified context.
    "belongsTo": "https://uri.fiware.org/ns/data-models#belongsTo",
    "hasAgriCrop": "https://uri.fiware.org/ns/data-models#hasAgriCrop",
    "refBuilding": "https://uri.fiware.org/ns/data-models#refBuilding",
    "numValue": SDM_TERM + "numValue", "unit": SDM_TERM + "unit", "measurementType": SDM_TERM + "measurementType",
    "maxTemperature": SDM_TERM + "maxTemperature", "minTemperature": SDM_TERM + "minTemperature",
    "juuriContextVersion": CONTEXT_VERSION,
}


def urn(kind: str, key: str) -> str:
    return f"urn:ngsi-ld:{kind}:{key}"


# ------------------------------------------------------------------------------ broker client
class Broker:
    def __init__(self, url: str = BROKER):
        self.url = url
        self.ngsi = url + "/ngsi-ld/v1"
        self.http = httpx.Client(timeout=60)
        self._ctx = None

    def alive(self) -> bool:
        try:
            return self.http.get(self.url + "/ngsi-ld/ex/v1/version", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def version(self) -> dict:
        return self.http.get(self.url + "/ngsi-ld/ex/v1/version").json()

    @property
    def context(self) -> str:
        """URL of the Juuri @context hosted in the broker (imports the Smart Data Models context)."""
        if self._ctx is None:
            self._ctx = self.ensure_context()
        return self._ctx

    def ensure_context(self) -> str:
        for u in self.http.get(self.ngsi + "/jsonldContexts").json():
            if "/jsonldContexts/" in u:
                body = self.http.get(self.ngsi + "/jsonldContexts/" + u.rsplit("/", 1)[1])
                if body.status_code == 200 and CONTEXT_VERSION in body.text:
                    return u
        r = self.http.post(self.ngsi + "/jsonldContexts", json={"@context": [JUURI_TERMS, SDM_CONTEXT]})
        r.raise_for_status()
        cid = r.headers["Location"].rsplit("/", 1)[1]
        return next(u for u in self.http.get(self.ngsi + "/jsonldContexts").json() if u.endswith(cid))

    def _h(self, json_body=True) -> dict:
        h = {"Link": f'<{self.context}>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"',
             "Accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def upsert(self, entities: list[dict], update_only: bool = False, chunk: int = 100) -> dict:
        ok, failed = 0, []
        for i in range(0, len(entities), chunk):
            part = entities[i:i + chunk]
            r = self.http.post(self.ngsi + "/entityOperations/upsert" + ("?options=update" if update_only else ""),
                               headers=self._h(), content=json.dumps(part, default=_json_default))
            if r.status_code in (200, 201, 204):
                ok += len(part)
            elif r.status_code == 207:
                body = r.json()
                ok += len(body.get("success", []))
                failed += body.get("errors", [])
            else:
                failed.append(dict(status=r.status_code, body=r.text[:500]))
        return dict(ok=ok, failed=failed)

    def patch(self, eid: str, attrs: dict) -> int:
        return self.http.patch(f"{self.ngsi}/entities/{eid}/attrs", headers=self._h(), content=json.dumps(attrs, default=_json_default)).status_code

    def append(self, eid: str, attrs: dict) -> int:
        """POST /entities/{id}/attrs: add or overwrite attributes (PATCH only updates existing ones)."""
        r = self.http.post(f"{self.ngsi}/entities/{eid}/attrs", headers=self._h(), content=json.dumps(attrs, default=_json_default))
        if r.status_code not in (204, 207):
            raise RuntimeError(f"append {eid}: HTTP {r.status_code} {r.text[:300]}")
        if r.status_code == 207:
            print(f"warning: partial update of {eid}: {r.text[:300]}", file=sys.stderr)
        return r.status_code

    def query(self, type_: str | None = None, q: str | None = None, attrs: str | None = None, near: tuple | None = None,
              max_distance_m: float | None = None, limit: int = 100, key_values: bool = True, ids: str | None = None) -> list[dict]:
        p = {"limit": min(limit, 1000)}
        if type_:
            p["type"] = type_
        if ids:
            p["id"] = ids
        if q:
            p["q"] = q
        if attrs:
            p["attrs"] = attrs
        if key_values:
            p["options"] = "keyValues"
        if near:
            p.update(georel=f"near;maxDistance=={int(max_distance_m or 5000)}", geometry="Point",
                     coordinates=json.dumps([near[1], near[0]]), geoproperty="location")
        out, offset = [], 0
        while True:
            p["offset"] = offset
            r = self.http.get(self.ngsi + "/entities", params=p, headers=self._h(False))
            r.raise_for_status()
            page = r.json()
            out += page
            if len(page) < p["limit"] or len(out) >= limit:
                return out[:limit]
            offset += len(page)

    def count(self, type_: str, q: str | None = None) -> int:
        p = {"type": type_, "limit": 1, "count": "true"}
        if q:
            p["q"] = q
        r = self.http.get(self.ngsi + "/entities", params=p, headers=self._h(False))
        return int(r.headers.get("NGSILD-Results-Count", 0))

    def subscribe(self, sid: str, entity_type: str, watched: list[str], description: str, uri: str = NOTIFY_URI) -> int:
        self.http.delete(f"{self.ngsi}/subscriptions/{sid}", headers=self._h(False))
        body = {"id": sid, "type": "Subscription", "description": description, "entities": [{"type": entity_type}],
                "watchedAttributes": watched,
                "notification": {"format": "keyValues", "endpoint": {"uri": uri, "accept": "application/json"}}}
        return self.http.post(self.ngsi + "/subscriptions", headers=self._h(), content=json.dumps(body)).status_code

    def subscriptions(self) -> list[dict]:
        return self.http.get(self.ngsi + "/subscriptions", headers=self._h(False)).json()


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, dt.date)):
        return o.isoformat()
    raise TypeError(type(o))


def prop(value, unit=None, observed=None, source=None, review=None, method=None):
    p = {"type": "Property", "value": value}
    if unit:
        p["unitCode"] = unit
    if observed:
        p["observedAt"] = observed
    if source:
        p["source"] = {"type": "Property", "value": source}
    if method:
        p["method"] = {"type": "Property", "value": method}
    if review:
        p["reviewStatus"] = {"type": "Property", "value": review}
    return p


def iso(t) -> str:
    """NGSI-LD timestamps: UTC, 'Z' suffix, no offset."""
    t = pd.Timestamp(t)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(obj):
    return {"type": "Relationship", "object": obj}


def geo(geometry):
    return {"type": "GeoProperty", "value": geometry}


def point(lat, lon):
    return geo({"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]})


# ------------------------------------------------------------------------------ entity builders
def _review_state(T) -> str:
    iss = T["_issues"]
    return "provisional" if (iss.applied & (iss.applied_as == "provisional")).any() else "approved"


def build_entities() -> list[dict]:
    from . import climate as CL, inventory as INV, nature as N
    from .data import raw
    r = raw()
    T = INV.canonical()
    tables = T["_tables"]
    review = _review_state(T)
    now = dt.datetime(2025, 12, 31, 23, 59).isoformat() + "Z"
    ents = []
    inv = INV.line_items(T, 2025)

    # Building (SDM Building): plants and head office, with 2025 Scope 1/2 results
    for _, s in r["sites"].iterrows():
        s1 = inv[(inv.scope == "scope1") & inv.line_item.str.startswith(s.site_id + ":")].tco2e.sum()
        s2 = inv[(inv.scope == "scope2_market") & (inv.line_item == s.site_id)].tco2e.sum()
        ents.append({"id": urn("Building", s.site_id), "type": "Building", "name": prop(s["name"]),
                     "category": prop(["industrial"] if s.site_id != "S-HQ" else ["office"]), "description": prop(s.activity),
                     "address": prop({"addressCountry": s.country}), "location": point(s.lat, s.lon),
                     "ghgScope1": prop(round(s1, 2), "TNE", now, f"inventory 2025 scope1 lines {s.site_id}:*", review, "GHG Protocol, AR6 GWP100"),
                     "ghgScope2Market": prop(round(s2, 2), "TNE", now, f"inventory 2025 scope2_market {s.site_id}", review, "residual mix after registry GoOs")})

    # AgriCrop (SDM): contract crops with 2041-2060 climate risk under SSP2-4.5
    risk = CL.c2_risk()
    for crop in C.APETIT_CROPS:
        rr = risk[(risk.crop == crop) & (risk.scenario == "ssp245") & (risk.region == "FI_SW")]
        e = {"id": urn("AgriCrop", crop), "type": "AgriCrop", "name": prop(crop.replace("_", " "))}
        if len(rr):
            e["yieldChange2041_2060"] = prop(float(rr.mean_yield_change_pct.iloc[0]), "P1", source="juuri climate model C2 (FI_SW)", method="ridge on weather stress indices, 2025 mix")
            e["badYearProbability2041_2060"] = prop(float(rr.p_bad_year.iloc[0]))
            e["climateScenario"] = prop("ssp245")
        ents.append(e)

    # WeatherObserved (SDM) per climate grid cell: latest historical day; streamed live by the simulator
    cells = r["climate_cells"]
    for _, c in cells.iterrows():
        ents.append({"id": urn("WeatherObserved", c.cell_id), "type": "WeatherObserved", "name": prop(f"Grid cell {c.cell_id}"),
                     "location": point(c.lat, c.lon), "dataProvider": prop("Since AI synthetic weather (FMI-calibrated)")})

    # Catchment (Juuri extension; no SDM model for WFD catchments) with SBTN-style priority
    pr = N.d1_priority().set_index("catchment_id")
    gj = N.catchments_geojson()
    for f in gj["features"]:
        cid = f["properties"]["catchment_id"]
        p = pr.loc[cid]
        ents.append({"id": urn("Catchment", cid), "type": "Catchment", "name": prop(cid), "location": geo(f["geometry"]),
                     "receivingWaterType": prop(p.receiving_water_type), "ecologicalStatus": prop(p.ecological_status_class),
                     "pSensitivity": prop(float(p.p_sensitivity)),
                     "phosphorusToWater": prop(round(float(p.apetit_p_to_water_kg), 2), "KGM", now, "juuri D1 field-to-water model", review),
                     "nitrogenToWater": prop(round(float(p.apetit_n_to_water_kg), 1), "KGM", now, "juuri D1 field-to-water model", review),
                     "priorityRank": prop(int(p.priority_rank), source="(P/median + 0.5 N/median) x state_weight x p_sensitivity"),
                     "priorityScore": prop(round(float(p.priority_score), 3))})

    # AgriFarm (SDM): corrected coordinates, 2025 farm emissions with provenance
    fd = T["farm_detail"]
    fd = fd[(fd.year == 2025) & (fd.region == "FI_SW")]
    farm_t = fd.groupby("farm_id").agg(t=("tco2e", "sum"), flag=("flag_tco2e", "sum"), org=("organic_share", "max"),
                                       src=("source", "first"), recs=("records", lambda x: ";".join(x)))
    loads = N.d1_loads()
    parcels_by_farm = loads.groupby("farm_id").parcel_id.apply(list)
    for _, f in tables["farm_registry"].iterrows():
        e = {"id": urn("AgriFarm", f.farm_id), "type": "AgriFarm", "name": prop(f"Contract farm {f.farm_id}"),
             "location": point(f.lat, f.lon), "description": prop(f"segment {f.segment}; contract crops {f.contract_crops}"),
             "address": prop({"addressCountry": f.country, "addressRegion": f.region})}
        if f.farm_id in parcels_by_farm.index:
            e["hasAgriParcel"] = rel([urn("AgriParcel", p) for p in parcels_by_farm[f.farm_id]])
        if f.farm_id in farm_t.index:
            ft = farm_t.loc[f.farm_id]
            e["farmEmissions"] = prop(round(float(ft.t), 2), "TNE", now, f"{ft.src}; deliveries {ft.recs[:200]}", review,
                                      "cradle-to-farm-gate, METHODOLOGY §5, organic soils included")
            e["flagEmissions"] = prop(round(float(ft.flag), 2), "TNE")
            e["organicSoilShare"] = prop(round(float(ft.org), 3), source="max(survey, soil map ~88 % accurate)", review=review)
        ents.append(e)

    # AgriParcel (SDM): 2025 contract-crop parcels with soil, catchment, weather cell and P/N to water
    from .climate import parcels as cparcels
    cp = cparcels().set_index("parcel_id")
    geoms = _parcel_geometries(set(loads.parcel_id))
    for _, p in loads.iterrows():
        e = {"id": urn("AgriParcel", p.parcel_id), "type": "AgriParcel", "name": prop(p.parcel_id),
             "area": prop(float(p.area_ha), "HAR"), "belongsTo": rel(urn("AgriFarm", p.farm_id)),
             "hasAgriCrop": rel(urn("AgriCrop", p.crop)), "soilClass": prop(p.soil_class_soilmap),
             "location": geo(geoms.get(p.parcel_id, {"type": "Point", "coordinates": [float(p.lon), float(p.lat)]})),
             "refCatchment": rel(urn("Catchment", p.catchment_id)), "refWeatherCell": rel(urn("WeatherObserved", cp.loc[p.parcel_id, "cell_id"])),
             "phosphorusToWater": prop(round(float(p.p_kg), 3), "KGM", now, "juuri D1 model", review),
             "nitrogenToWater": prop(round(float(p.n_kg), 2), "KGM", now, "juuri D1 model", review),
             "droughtAlert": prop("none"), "waterDeficitToDate": prop(0.0, "MMT")}
        ents.append(e)

    # DeviceMeasurement (SDM) per site x metered carrier: latest reading; replayed live by the simulator
    em = r["energy_meter"]
    for (site, carrier), g in em.groupby(["site_id", "carrier"]):
        ents.append({"id": urn("DeviceMeasurement", f"{site}:{carrier}"), "type": "DeviceMeasurement",
                     "name": prop(f"{site} {carrier} meter"), "controlledProperty": prop("energy"),
                     "measurementType": prop(carrier), "unit": prop("MWH"), "refDevice": rel(urn("Device", f"{site}:{carrier}")),
                     "refBuilding": rel(urn("Building", site))})
    return ents


def _parcel_geometries(ids: set) -> dict:
    p = C.DATA / "06_nature" / "parcels.geojson"
    out = {}
    for f in json.loads(p.read_text())["features"]:
        if f["properties"]["parcel_id"] in ids:
            out[f["properties"]["parcel_id"]] = f["geometry"]
    return out


SUBSCRIPTIONS = [
    ("urn:ngsi-ld:Subscription:juuri-meters", "DeviceMeasurement", ["dateObserved"],
     "Juuri live data-quality check on every new meter reading"),
    ("urn:ngsi-ld:Subscription:juuri-weather", "WeatherObserved", ["dateObserved"],
     "Juuri season-to-date water balance -> drought impact on contract parcels"),
]


def load(b: Broker | None = None) -> dict:
    b = b or Broker()
    if not b.alive():
        sys.exit(f"No context broker at {BROKER}. Start it with `python -m juuri fiware up`.")
    print(f"Context: {b.context}")
    ents = build_entities()
    res = b.upsert(ents)
    by_type = pd.Series([e["type"] for e in ents]).value_counts().to_dict()
    subs = {sid: b.subscribe(sid, t, w, d) for sid, t, w, d in SUBSCRIPTIONS}
    print(json.dumps(dict(entities=by_type, upserted=res["ok"], failed=len(res["failed"]), subscriptions=subs), indent=1))
    if res["failed"]:
        print(json.dumps(res["failed"][:3], indent=1)[:1500])
    return res


# ------------------------------------------------------------------------------ live reactions
class LiveEngine:
    """State and rules that react to notifications from the broker."""

    def __init__(self, broker: Broker | None = None):
        from . import climate as CL, inventory as INV
        self.b = broker or Broker()
        self.lock = threading.Lock()
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        T = INV.canonical()
        en = T["energy"]
        en = en[(en.month > 0) & (en.year < 2025)]
        self.norm = en.groupby(["site_id", "carrier", "month"]).mwh.median().to_dict()
        self.cells: dict[str, dict] = {}
        # parcels for drought impact, with the features the yield model needs
        from . import nature as N
        x = N.d1_loads()[["parcel_id", "farm_id", "crop", "catchment_id", "area_ha"]].copy()
        cp = CL.parcels().set_index("parcel_id")
        for c in ["cell_id", "clay_pct", "organic_share", "coarse_share", "region"]:
            x[c] = x.parcel_id.map(cp[c])
        irr = CL.training_data().groupby(["farm_id", "crop"]).irrigated.mean()
        x["irrigated"] = [float(irr.get((f, c), 0.0) > 0.5) for f, c in zip(x.farm_id, x.crop)]
        x["trend"] = 2026 - 2020
        wf = CL.weather_features()
        clim = wf[(wf.scenario == "historical") & wf.year.between(1991, 2020)].groupby("cell_id").mean(numeric_only=True)
        self.clim = clim
        self.parcels = x
        self.model = CL.model()
        self.last_alert: dict[str, str] = {}
        self.log_file = LIVE_DIR / "events.jsonl"

    def log(self, kind: str, **kw):
        rec = dict(ts=dt.datetime.now().isoformat(timespec="seconds"), kind=kind, **kw)
        with self.lock, open(self.log_file, "a") as f:
            f.write(json.dumps(rec, default=_json_default) + "\n")

    # -- meters
    def on_meter(self, e: dict):
        site, carrier = e["id"].split(":")[-2:]
        when = pd.Timestamp(e.get("dateObserved", "2025-01-31"))
        when = when.tz_convert("UTC").tz_localize(None) if when.tzinfo is not None else when
        val = float(e.get("numValue", np.nan))
        norm = self.norm.get((site, carrier, when.month))
        rk = f"energy:{site}:{when.year}-{when.month:02d}:{carrier}"
        if not norm or not np.isfinite(val) or norm <= 0:
            self.log("meter", record_key=rk, value=val, verdict="no reference")
            return
        ratio = val / norm
        issue = None
        if ratio > 200:
            issue = ("unit_error", round(val / 1000, 3), 0.95, f"Reading is {ratio:,.0f}x the {when.strftime('%B')} norm: kWh in the MWh field.")
        elif ratio > 2.5:
            issue = ("meter_spike", round(norm, 3), 0.8, f"Reading is {ratio:.1f}x the {when.strftime('%B')} median of 2021-2024; seasonal estimate proposed.")
        elif ratio < 0.2:
            issue = ("outlier_implausible", round(norm, 3), 0.6, f"Reading is only {ratio:.0%} of the seasonal norm: partial month or meter fault?")
        self.log("meter", record_key=rk, value=val, norm=round(norm, 2), ratio=round(ratio, 3), verdict=issue[0] if issue else "ok")
        if issue:
            itype, proposed, conf, why = issue
            iid = urn("DataQualityIssue", rk)
            self.b.upsert([{"id": iid, "type": "DataQualityIssue", "issueType": prop(itype), "recordKey": prop(rk),
                            "observedValue": prop(val, "MWH"), "proposedValue": prop(proposed, "MWH"), "confidence": prop(conf),
                            "rule": prop("LIVE-" + itype.upper()), "explanation": prop(why), "reviewStatus": prop("pending"),
                            "refMeasurement": rel(e["id"]), "detectedAt": prop(iso(dt.datetime.utcnow()))}])

    # -- weather
    def on_weather(self, e: dict):
        cid = e["id"].split(":")[-1]
        d = pd.Timestamp(e.get("dateObserved")).tz_localize(None) if pd.Timestamp(e.get("dateObserved")).tzinfo is None else pd.Timestamp(e.get("dateObserved")).tz_convert("UTC").tz_localize(None)
        if d.month < 5 or d.month > 8:
            return
        s = self.cells.setdefault(cid, {"cwb": {5: 0.0, 6: 0.0, 7: 0.0, 8: 0.0}, "hot": {6: 0, 7: 0, 8: 0}, "date": None, "gaps": 0})
        if s["date"] is not None and d <= s["date"]:
            return
        # Missed observations (station outage or dropped notification): fill with the cell's
        # climatological daily water balance for that month, and count them.
        start = s["date"] + pd.Timedelta(days=1) if s["date"] is not None else pd.Timestamp(d.year, 5, 1)
        for g in pd.date_range(start, d - pd.Timedelta(days=1)):
            if 5 <= g.month <= 8 and cid in self.clim.index:
                s["cwb"][g.month] += self.clim.loc[cid, f"cwb_{['may', 'jun', 'jul', 'aug'][g.month - 5]}"] / g.days_in_month
                s["gaps"] += 1
        s["cwb"][d.month] += float(e.get("precipitation", 0)) - float(e.get("potentialEvapotranspiration", 0))
        if d.month in s["hot"] and float(e.get("maxTemperature", 0)) >= 27:
            s["hot"][d.month] += 1
        s["date"] = d
        if d.dayofweek == 6 or d.day == pd.Timestamp(d).days_in_month:   # weekly re-estimate per cell
            self.reassess(cid, d)

    def reassess(self, cid: str, d: pd.Timestamp):
        s = self.cells[cid]
        px = self.parcels[self.parcels.cell_id == cid].copy()
        if px.empty or cid not in self.clim.index:
            return
        base = self.clim.loc[cid]
        for c in base.index:
            px[c] = base[c]
        cur = px.copy()
        names = {5: "cwb_may", 6: "cwb_jun", 7: "cwb_jul", 8: "cwb_aug"}
        for m, col in names.items():
            if m < d.month:
                cur[col] = s["cwb"][m]
            elif m == d.month:   # month in progress: observed so far + climatological remainder
                frac = d.day / d.days_in_month
                cur[col] = s["cwb"][m] + (1 - frac) * base[col]
        for m, col in {6: "hot_jun", 7: "hot_jul", 8: "hot_aug"}.items():
            if m <= d.month:
                cur[col] = max(s["hot"][m], base[col] * (1 if m < d.month else 1 - d.day / d.days_in_month) + s["hot"][m] * 0)
        y0 = self.model.predict(px.reset_index(drop=True))
        y1 = self.model.predict(cur.reset_index(drop=True))
        impact = 100 * (y1 / y0 - 1)
        deficit = -sum(min(v, 0) for v in s["cwb"].values())
        updates, changed = [], 0
        for pid, imp in zip(px.parcel_id, impact):
            if not np.isfinite(imp):
                continue
            level = "high" if imp <= -15 else "moderate" if imp <= -7 else "none"
            updates.append({"id": urn("AgriParcel", pid), "type": "AgriParcel",
                            "waterDeficitToDate": prop(round(deficit, 1), "MMT", iso(d), f"WeatherObserved {cid} since 1 May"),
                            "expectedYieldImpact": prop(round(float(imp), 1), "P1", iso(d), "juuri yield model, season-to-date weather vs 1991-2020", None,
                                                        "ridge on stress indices; remaining season at climatology"),
                            "droughtAlert": prop(level, observed=iso(d))})
            if self.last_alert.get(pid) != level:
                changed += 1
                self.last_alert[pid] = level
        self.b.upsert(updates, update_only=True)
        self.log("drought", cell=cid, date=d.date().isoformat(), parcels=len(updates), alerts_changed=changed, gap_filled_days=s["gaps"],
                 mean_impact_pct=round(float(np.nanmean(impact)), 2), deficit_mm=round(deficit, 1))


def listen(port: int = LISTEN_PORT, engine: LiveEngine | None = None):
    engine = engine or LiveEngine()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            for e in body.get("data", []):
                try:
                    t = e.get("type", "")
                    if t.endswith("DeviceMeasurement"):
                        engine.on_meter(e)
                    elif t.endswith("WeatherObserved"):
                        engine.on_weather(e)
                except Exception as ex:  # noqa: BLE001 - keep the listener alive, log the failure
                    engine.log("error", entity=e.get("id"), error=str(ex))

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 256   # the broker delivers notifications in bursts
        daemon_threads = True

    srv = Server(("0.0.0.0", port), H)
    print(f"Juuri listening for NGSI-LD notifications on :{port} (broker -> {NOTIFY_URI})", flush=True)
    srv.serve_forever()


# ------------------------------------------------------------------------------ simulator
@lru_cache(maxsize=1)
def _weather_2026() -> pd.DataFrame:
    w = pd.read_parquet(C.DATA / "04_climate" / "weather_daily_1991_2060.parquet",
                        filters=[("scenario", "==", "historical")])
    return w[(w.date >= "2026-05-01") & (w.date <= "2026-08-31")]


def simulate(days: int = 123, start: str = "2026-05-01", speed: float = 0.0, pace: float = 0.03, b: Broker | None = None,
             meters: bool = True):
    """Stream daily weather (2026 season) per station and replay the raw 2025 meter feed (one month per ~10 days).

    Each station PATCHes its own observation, ``pace`` seconds apart, as field devices would; ``speed`` is an
    extra pause per simulated day."""
    from .data import raw
    b = b or Broker()
    w = _weather_2026()
    dates = [d for d in sorted(w.date.unique()) if d >= pd.Timestamp(start)][:days]
    em = raw()["energy_meter"]
    em25 = em[em.year == 2025].sort_values(["month", "site_id", "carrier"])
    month = 1
    for i, d in enumerate(dates):
        t = iso(d)
        for r in w[w.date == d].itertuples():
            b.append(urn("WeatherObserved", r.cell_id), {
                "dateObserved": prop(t), "temperature": prop(round(r.tmean_c, 1), "CEL", t),
                "maxTemperature": prop(round(r.tmax_c, 1), "CEL", t), "minTemperature": prop(round(r.tmin_c, 1), "CEL", t),
                "precipitation": prop(round(r.precip_mm, 1), "MMT", t), "solarRadiation": prop(round(r.rad_mj_m2, 2)),
                "potentialEvapotranspiration": prop(round(r.pet_mm, 2), "MMT", t)})
            time.sleep(pace)
        if meters and i % 10 == 9 and month <= 12:
            m = em25[em25.month == month]
            for r in m.itertuples():
                ts = f"2025-{month:02d}-28T00:00:00Z"
                b.append(urn("DeviceMeasurement", f"{r.site_id}:{r.carrier}"), {
                    "numValue": prop(float(r.value), "MWH", ts, f"energy_meter_readings.csv {r.record_key}"), "dateObserved": prop(ts)})
                time.sleep(pace)
            print(f"  meters: replayed 2025-{month:02d} ({len(m)} readings)", flush=True)
            month += 1
        if i % 7 == 0:
            print(f"{pd.Timestamp(d).date()} weather from {w[w.date == d].cell_id.nunique()} stations", flush=True)
        time.sleep(speed)


def status(b: Broker | None = None) -> dict:
    b = b or Broker()
    if not b.alive():
        return dict(alive=False, broker=BROKER)
    types = ["Building", "AgriFarm", "AgriParcel", "AgriCrop", "WeatherObserved", "DeviceMeasurement", "Catchment", "DataQualityIssue"]
    counts = {t: b.count(t) for t in types}
    subs = [dict(id=s.get("id"), status=s.get("status"), timesSent=s.get("notification", {}).get("timesSent"),
                 lastSuccess=s.get("notification", {}).get("lastSuccess"), lastFailure=s.get("notification", {}).get("lastFailure"))
            for s in b.subscriptions()]
    return dict(alive=True, broker=BROKER, version=b.version().get("Orion-LD version"), context=b.context, entities=counts,
                subscriptions=subs, drought_alerts={lv: b.count("AgriParcel", f'droughtAlert=="{lv}"') for lv in ("moderate", "high")})


def read_events(n: int = 200) -> pd.DataFrame:
    f = LIVE_DIR / "events.jsonl"
    if not f.exists():
        return pd.DataFrame()
    lines = f.read_text().splitlines()[-n:]
    return pd.DataFrame([json.loads(x) for x in lines])


def _sim_args(a: list[str]) -> dict:
    """simulate/demo options: [start=2026-05-01] [days=123] [pace=0.03]"""
    kw = dict(x.split("=", 1) for x in a if "=" in x)
    return dict(start=kw.get("start", "2026-05-01"), days=int(kw.get("days", 123)), pace=float(kw.get("pace", 0.03)))


def main(argv: list[str]):
    cmd = argv[0] if argv else "status"
    if cmd == "up":
        subprocess.run(["docker", "compose", "-f", str(COMPOSE), "up", "-d", "--wait"], check=True)
    elif cmd == "down":
        subprocess.run(["docker", "compose", "-f", str(COMPOSE), "down"], check=True)
    elif cmd == "load":
        load()
    elif cmd == "listen":
        listen()
    elif cmd == "simulate":
        simulate(**_sim_args(argv[1:]))
    elif cmd == "demo":
        eng = LiveEngine()
        threading.Thread(target=listen, kwargs=dict(engine=eng), daemon=True).start()
        time.sleep(1)
        simulate(**_sim_args(argv[1:]))
        time.sleep(3)
        print(json.dumps(status(), indent=1, default=str))
    else:
        print(json.dumps(status(), indent=1, default=str))
