"""Juuri web product: FastAPI backend serving the JSON API and the single-page app in web/.

    python -m juuri serve            # http://localhost:8000
"""
from __future__ import annotations

import json
import math
import threading
import warnings
from typing import Optional

warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import config as C  # noqa: E402

WEB = C.REPO_ROOT / "web"
app = FastAPI(title="Juuri API", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ------------------------------------------------------------------------------ helpers
def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if (math.isnan(o) or math.isinf(o)) else round(float(o), 6)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    return o


def J(obj) -> Response:
    return Response(json.dumps(clean(obj), ensure_ascii=False, default=str), media_type="application/json")


def recs(df: pd.DataFrame, cols: list[str] | None = None) -> list[dict]:
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    return df.astype(object).where(pd.notna(df), None).to_dict("records")


def canonical(policy: str = "provisional"):
    from . import inventory as INV
    return INV.canonical("strict" if policy == "strict" else "provisional")


_LINEAGE: dict = {}


def lineage_cached(T, year: int) -> pd.DataFrame:
    from . import inventory as INV
    key = (id(T), year)
    if key not in _LINEAGE:
        _LINEAGE.clear() if len(_LINEAGE) > 8 else None
        _LINEAGE[key] = INV.lineage(T, year)
    return _LINEAGE[key]


def review_state(T) -> dict:
    iss = T["_issues"]
    applied = iss[iss.applied]
    return dict(provisional=int((applied.applied_as == "provisional").sum()), applied=int(iss.applied.sum()), issues=len(iss),
                approved=int((iss.status == "approved").sum()), rejected=int((iss.status == "rejected").sum()))


@app.on_event("startup")
def warm():
    def _w():
        try:
            from . import climate as CL, nature as N, pathways as P
            lineage_cached(canonical(), 2025)
            P.base_inventory()
            N.d1_priority()
            CL.c3_euro_risk()
            CL.backtest(2019)
        except Exception as e:  # noqa: BLE001
            print("warm-up failed:", e)
    threading.Thread(target=_w, daemon=True).start()


# ------------------------------------------------------------------------------ meta & overview
@app.get("/api/meta")
def meta():
    from . import assistant as A
    from . import fiware as FW
    T = canonical()
    return J(dict(languages=["en", "fi", "sv"], assistant="claude" if A.has_credentials() else "offline",
                  broker=FW.Broker().alive(), review=review_state(T)))


@app.get("/api/overview")
def overview(policy: str = "provisional"):
    from . import inventory as INV, pathways as P
    from .data import raw
    T = canonical(policy)
    s25 = INV.summary(T, 2025).set_index("category").tco2e
    fl = INV.flag_split(T, 2025)
    tot = s25["scope1_total"] + s25["scope2_market_total"] + s25["scope3_total"]
    fd = T["farm_detail"]
    fd = fd[(fd.year == 2025) & (fd.region == "FI_SW")]
    trend = []
    for y in C.INVENTORY_YEARS:
        s = INV.summary(T, y).set_index("category").tco2e
        trend.append(dict(year=y, scope1=s["scope1_total"], scope2=s["scope2_market_total"], scope3=s["scope3_total"]))
    pub = raw()["published"]
    published = [dict(year=int(r.year), total=float(r.scope1 + r.scope2_market + (r.scope3_restated if pd.notna(r.scope3_restated) else r.scope3)))
                 for r in pub.itertuples()]
    b = P.base_inventory("strict" if policy == "strict" else "provisional")
    m = P.macc(b)
    top = m[m.abatement_tco2e > 500].sort_values("cost_eur_per_t_scenario").head(4)
    return J(dict(kpi=dict(scope1=s25["scope1_total"], scope2m=s25["scope2_market_total"], scope2l=s25["scope2_location_total"],
                           scope3=s25["scope3_total"], biogenic=s25["biogenic_co2_scope1"], flag_share=100 * fl.flag_tco2e.sum() / tot,
                           peat_share=100 * fd.organic_tco2e.sum() / fd.tco2e.sum()),
                  trend=trend, published=published, review=review_state(T),
                  actions=recs(top, ["lever_id", "name", "abatement_tco2e", "cost_eur_per_t_scenario", "owner", "confidence", "abatement_basis"])))


# ------------------------------------------------------------------------------ data quality & review
@app.get("/api/issues")
def issues(policy: str = "provisional", year: Optional[int] = None, table: Optional[str] = None, issue_type: Optional[str] = None,
           status: Optional[str] = None, limit: int = 2000):
    iss = canonical(policy)["_issues"]
    facets = dict(years=sorted(int(y) for y in iss.year.unique()), tables=sorted(iss.table.unique()), types=sorted(iss.issue_type.unique()))
    x = iss
    if year:
        x = x[x.year == year]
    if table:
        x = x[x.table == table]
    if issue_type:
        x = x[x.issue_type == issue_type]
    if status:
        x = x[x.status == status]
    by_type = x.groupby("issue_type").size().sort_values().to_dict()
    cols = ["issue_id", "table", "record_key", "year", "issue_type", "field", "old_value", "new_value", "confidence", "status",
            "applied_as", "rule", "explanation", "evidence", "reviewer"]
    return J(dict(total=len(x), facets=facets, by_type=by_type, rows=recs(x.head(limit), cols)))


class Decision(BaseModel):
    items: list[dict]
    decision: str
    reviewer: str
    comment: str = ""


@app.post("/api/decisions")
def decide(d: Decision):
    from . import review as RV
    if d.decision not in ("approved", "rejected") or not d.reviewer.strip():
        raise HTTPException(400, "decision must be approved|rejected and reviewer is required")
    RV.save_decisions([dict(record_key=i["record_key"], field=i["field"], decision=d.decision, reviewer=d.reviewer.strip(),
                            comment=d.comment) for i in d.items])
    return J(dict(saved=len(d.items)))


@app.get("/api/audit.csv")
def audit_csv(policy: str = "provisional"):
    return PlainTextResponse(canonical(policy)["_audit"].to_csv(index=False), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=audit_log.csv"})


@app.get("/api/issues.csv")
def issues_csv(policy: str = "provisional"):
    return PlainTextResponse(canonical(policy)["_issues"].to_csv(index=False), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=issues.csv"})


# ------------------------------------------------------------------------------ inventory
@app.get("/api/inventory")
def inventory(year: int = 2025, policy: str = "provisional"):
    from . import inventory as INV
    T = canonical(policy)
    inv = INV.line_items(T, year)
    s = INV.summary(T, year).set_index("category").tco2e
    cat = inv[inv.scope != "scope2_location"].groupby(["scope", "category"]).tco2e.sum().reset_index()
    fl = INV.flag_split(T, year)
    from .data import raw
    cur = raw()["current_calc"]
    fd = T["farm_detail"]
    fd = fd[(fd.year == year) & (fd.region == "FI_SW")]
    m = fd.merge(cur, on=["farm_id", "crop", "year"], how="left")
    m["current_t"] = m.carbon_footprint_kgco2e_per_kg * m.delivered_kg / 1000
    peat = m.groupby("crop").agg(juuri_t=("tco2e", "sum"), current_t=("current_t", "sum"), organic_t=("organic_tco2e", "sum"),
                                 delivered_t=("delivered_kg", lambda x: x.sum() / 1000)).reset_index()
    return J(dict(totals=dict(scope1=s["scope1_total"], scope2l=s["scope2_location_total"], scope2m=s["scope2_market_total"],
                              scope3=s["scope3_total"], biogenic=s["biogenic_co2_scope1"]),
                  categories=recs(cat), flag=fl.flag_tco2e.sum(), nonflag=fl[fl.scope == "scope3"].non_flag_tco2e.sum(),
                  lines=recs(inv.sort_values("tco2e", ascending=False), ["scope", "category", "line_item", "tco2e", "basis"]),
                  peat=recs(peat), review=review_state(T)))


@app.get("/api/lineage")
def lineage(line_item: str, scope: str, year: int = 2025, policy: str = "provisional"):
    from . import inventory as INV
    T = canonical(policy)
    lin = lineage_cached(T, year)
    lin = lin[(lin.line_item == line_item) & (lin.scope == scope)].sort_values("tco2e", ascending=False)
    audit = T["_audit"]
    keys = set()
    for k in lin.record_key:
        keys.add(k)
        keys.update(k.split(" <- ")[-1].split(";"))
    touched = audit[audit.record_key.isin(keys)] if "record_key" in audit.columns else pd.DataFrame()   # strict mode: no corrections applied
    return J(dict(records=recs(lin.head(500)), n=len(lin), total=lin.tco2e.sum(),
                  corrections=recs(touched, ["record_key", "field", "old_value", "new_value", "issue_type", "rule_or_model", "status", "approved_by"])))


# ------------------------------------------------------------------------------ targets & pathways
@app.get("/api/targets")
def targets(scenario: str = "net_zero_2050", compare: str = "current_policies", target_year: int = 2032,
            levers: Optional[str] = None, policy: str = "provisional"):
    from . import pathways as P
    from .data import raw
    b = P.base_inventory("strict" if policy == "strict" else "provisional")
    lv = P.lever_table(b)
    ab = lv[lv.kind == "abatement"]
    chosen = levers.split(",") if levers else list(ab[ab.confidence.isin(["high", "medium"])].lever_id)
    pw = {sc: recs(P.pathway(b, sc, chosen, end=2035, target_year=target_year)) for sc in {scenario, compare}}
    m = P.macc(b, scenario)
    m = m[m.lever_id.isin(chosen)].copy()
    m["x_end"] = m.abatement_tco2e.cumsum()
    m["x_start"] = m.x_end - m.abatement_tco2e
    return J(dict(scenarios=sorted(raw()["scenarios"].scenario.unique()), chosen=chosen,
                  base_years=recs(P.base_year_options(b)), targets=recs(P.targets(b, target_year=target_year)), pathways=pw,
                  macc=recs(m, ["lever_id", "name", "boundary", "abatement_tco2e", "cost_eur_per_t_scenario", "uncertainty_frac", "x_start",
                                "x_end", "abatement_basis", "owner", "confidence"]),
                  levers=recs(lv, ["lever_id", "name", "kind", "boundary", "abatement_tco2e", "abatement_basis", "cost_eur_per_t", "confidence",
                                   "earliest_start", "owner", "co_benefits", "trade_offs"]),
                  coverage=recs(P.scope3_coverage(b), ["category", "tco2e", "share_of_scope3", "must_be_covered_v2"])))


# ------------------------------------------------------------------------------ climate
@app.get("/api/climate")
def climate():
    from . import climate as CL
    return J(dict(risk=recs(CL.c2_risk()), eur=recs(CL.c3_euro_risk()), backtest=recs(CL.backtest(2019))))


@app.get("/api/climate/series")
def climate_series(crop: str = "turnip_rape", region: str = "FI_SW"):
    from . import climate as CL
    cy = CL.climate_yields()
    x = cy[(cy.crop == crop) & (cy.region == region)].sort_values("year")
    base = x[(x.scenario == "historical") & x.year.between(1991, 2020)].yield_t_ha.mean()
    out = {}
    for sc, g in x.groupby("scenario"):
        out[sc] = dict(year=g.year.tolist(), y=g.yield_t_ha.round(3).tolist(),
                       smooth=g.yield_t_ha.rolling(9, center=True, min_periods=4).mean().round(3).tolist())
    return J(dict(series=out, bad_year=0.8 * base, regions=sorted(cy[cy.crop == crop].region.unique())))


@app.get("/api/climate/irrigation")
def irrigation(crop: str = "potato", scenario: str = "ssp585"):
    from . import climate as CL
    return J(CL.adaptation_irrigation(crop, "FI_SW", scenario))


# ------------------------------------------------------------------------------ nature
@app.get("/api/nature")
def nature():
    from . import nature as N
    cal = N.calibration()
    return J(dict(priority=recs(N.d1_priority()), hotspots=recs(N.parcel_hotspots(30)), materiality=recs(N.d2_screening(), [
        "material", "origin_country", "t", "tco2e", "materiality_score", "why", "sbtn_high_impact", "eudr_commodity", "water_stress_cat",
        "deforestation_risk", "biodiversity_sensitivity"]), calibration=cal))


@app.get("/api/nature/map")
def nature_map():
    from . import nature as N
    pr = N.d1_priority().set_index("catchment_id")
    gj = json.loads(json.dumps(N.catchments_geojson()))
    for f in gj["features"]:
        cid = f["properties"]["catchment_id"]
        f["id"] = cid
        f["properties"].update(priority_rank=int(pr.priority_rank.get(cid, 0)), priority_score=float(pr.priority_score.get(cid, 0)),
                               p_kg=float(pr.apetit_p_to_water_kg.get(cid, 0)))
    loads = N.d1_loads()
    pts = recs(loads.assign(p_kg=loads.p_kg.round(2)), ["parcel_id", "farm_id", "crop", "lat", "lon", "area_ha", "p_kg", "catchment_id"])
    return J(dict(catchments=gj, parcels=pts))


# ------------------------------------------------------------------------------ live context (FIWARE)
@app.get("/api/live")
def live():
    from . import fiware as FW
    b = FW.Broker()
    if not b.alive():
        return J(dict(alive=False))
    st = FW.status(b)
    iss = b.query("DataQualityIssue", limit=500)
    par = pd.DataFrame(b.query("AgriParcel", attrs="expectedYieldImpact,droughtAlert,waterDeficitToDate,area,hasAgriCrop", limit=5000))
    drought = []
    if "expectedYieldImpact" in par.columns:
        from .climate import parcels as cp
        reg = cp().set_index("parcel_id").region
        par["crop"] = par.hasAgriCrop.str.split(":").str[-1]
        par["region"] = par.id.str.split(":").str[-1].map(reg)
        par = par.dropna(subset=["expectedYieldImpact"])
        for (r, c), g in par.groupby(["region", "crop"]):
            drought.append(dict(region=r, crop=c, parcels=len(g), area_ha=g.area.sum(),
                                impact_pct=(g.expectedYieldImpact * g.area).sum() / g.area.sum(),
                                moderate=int((g.droughtAlert == "moderate").sum()), high=int((g.droughtAlert == "high").sum()),
                                deficit_mm=g.waterDeficitToDate.mean()))
    wo = b.query("WeatherObserved", attrs="dateObserved", limit=200)
    last = max((w.get("dateObserved") for w in wo if w.get("dateObserved")), default=None)
    return J(dict(alive=True, status=st, issues=iss, drought=drought, last_observation=last, events=recs(FW.read_events(100).iloc[::-1]) if len(FW.read_events(1)) else []))


class LiveDecision(BaseModel):
    ids: list[str]
    record_keys: list[str]
    decision: str
    reviewer: str


@app.post("/api/live/decision")
def live_decision(d: LiveDecision):
    from . import fiware as FW, review as RV
    b = FW.Broker()
    for eid, rk in zip(d.ids, d.record_keys):
        b.patch(eid, {"reviewStatus": FW.prop(d.decision, source=f"reviewer {d.reviewer}")})
        RV.save_decision(rk, "value", d.decision, d.reviewer, "live review (FIWARE)")
    return J(dict(saved=len(d.ids)))


@app.post("/api/live/advance")
def live_advance(days: int = 7):
    from . import fiware as FW
    b = FW.Broker()
    wo = b.query("WeatherObserved", attrs="dateObserved", limit=200)
    last = max((w.get("dateObserved") for w in wo if w.get("dateObserved")), default=None)
    start = (pd.Timestamp(last).tz_localize(None) + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if last else "2026-05-01"
    FW.simulate(days=min(days, 31), start=start, meters=False, b=b)
    return J(dict(start=start, days=days))


# ------------------------------------------------------------------------------ assistant & reports
class ChatIn(BaseModel):
    message: str
    lang: str = "en"
    conversation_id: Optional[str] = None


@app.post("/api/chat")
def chat(c: ChatIn):
    from . import assistant as A
    if not c.message.strip():
        raise HTTPException(400, "empty message")
    return J(A.chat(c.message, c.lang, c.conversation_id))


class ReportIn(BaseModel):
    kind: str
    lang: str = "en"
    params: dict = {}


@app.get("/api/reports")
def reports_list():
    from . import reports as R
    return J(dict(kinds=list(R.STANDARD), reports=R.list_reports()))


@app.post("/api/reports")
def reports_make(r: ReportIn):
    from . import reports as R
    if r.kind not in R.STANDARD:
        raise HTTPException(400, f"unknown report kind {r.kind}")
    try:
        rep = R.standard(r.kind, r.lang, **r.params)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    return J(dict(id=rep["id"], title=rep["title"]))


@app.get("/api/reports/{rid}.html", response_class=HTMLResponse)
def report_html(rid: str):
    from . import reports as R
    try:
        return HTMLResponse(R.render_html(R.load(rid)))
    except FileNotFoundError:
        raise HTTPException(404, "report not found")


@app.get("/api/reports/{rid}.md")
def report_md(rid: str):
    from . import reports as R
    try:
        rep = R.load(rid)
    except FileNotFoundError:
        raise HTTPException(404, "report not found")
    return PlainTextResponse(R.render_md(rep), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="juuri-{rep["kind"]}-{rid}.md"'})


# ------------------------------------------------------------------------------ method
@app.get("/api/method")
def method():
    import markdown as md
    scores = C.OUTPUTS / "dev_scores.json"
    return J(dict(html=md.markdown((C.REPO_ROOT / "docs" / "METHOD_NOTE.md").read_text(), extensions=["tables"]),
                  scores=json.loads(scores.read_text()) if scores.exists() else None))


# ------------------------------------------------------------------------------ static single-page app
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
