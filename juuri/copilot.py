"""Track E: copilot that answers with evidence.

Two layers:
1. ``answer_e()``: deterministic, auditable answers to the challenge questions. Each answer comes
   with the record keys / files / framework sections it was computed from.
2. ``Copilot``: a Claude agent (Anthropic Messages API, tool use) whose only tools are Juuri's
   own functions - inventory, lineage, issue log, pathways, climate risk, catchments and a search
   over the framework digest and method docs. It is instructed to cite the evidence its tools return.
   It needs ``ANTHROPIC_API_KEY`` (or another Anthropic credential); without it the app falls back
   to layer 1 plus document search.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from functools import lru_cache

import numpy as np
import pandas as pd

from . import config as C
from . import inventory as INV
from .data import raw

C.use_reference_tools()
from ccn_tools import factors as F  # noqa: E402

MODEL = "claude-opus-5-5"


# ------------------------------------------------------------------------------ document search
@lru_cache(maxsize=1)
def _chunks() -> list[dict]:
    from . import store
    files = [store.path("07_frameworks/framework_digest.md")] + sorted((C.PACKAGE_ROOT / "docs").glob("*.md"))
    out = []
    for f in files:
        text = f.read_text()
        parts = re.split(r"\n(?=#{1,3} )", text)
        for p in parts:
            title = p.strip().splitlines()[0].lstrip("# ").strip() if p.strip() else ""
            for j, para in enumerate(re.split(r"\n(?=\* \*\*)", p)):
                if len(para.strip()) > 40:
                    out.append(dict(source=str(f.relative_to(C.PACKAGE_ROOT) if C.PACKAGE_ROOT in f.parents else f.relative_to(C.REPO_ROOT)), section=title, text=para.strip()))
    return out


def _tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9äöå]+", s.lower())


def search_docs(query: str, k: int = 5) -> list[dict]:
    """BM25 over framework digest + methodology docs. Returns source file and section for citation."""
    ch = _chunks()
    docs = [_tok(c["section"] + " " + c["text"]) for c in ch]
    df = Counter(t for d in docs for t in set(d))
    n, avg = len(docs), np.mean([len(d) for d in docs])
    q = _tok(query)
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores.append(s)
    idx = np.argsort(scores)[::-1][:k]
    return [dict(**ch[i], score=round(float(scores[i]), 2)) for i in idx if scores[i] > 0]


# ------------------------------------------------------------------------------ deterministic answers
def _li(inv, **kw):
    x = inv
    for k, v in kw.items():
        x = x[x[k].str.contains(v, regex=True)] if isinstance(v, str) else x[x[k] == v]
    return x


@lru_cache(maxsize=1)
def answer_e() -> pd.DataFrame:
    T = INV.canonical()
    inv = INV.line_items(T, 2025)
    s = INV.summary(T, 2025).set_index("category").tco2e
    fl = INV.flag_split(T, 2025)
    fd = T["farm_detail"]
    fd25 = fd[(fd.year == 2025) & (fd.region == "FI_SW")]
    A = []

    def add(q, v, ev):
        A.append(dict(question_id=q, answer=v if isinstance(v, str) else f"{round(float(v), 4):g}", evidence=ev))

    x = inv[(inv.scope == "scope1") & inv.line_item.isin(["S-SAK:lng", "S-SAK:lpg"])]
    add("E01", x.tco2e.sum(), "inventory 2025 scope1 lines S-SAK:lng + S-SAK:lpg; fuel_deliveries.csv (fuel:FD*) - stock change, NCV 13.5/12.8 MWh/t, EF 0.1991/0.2336 t/MWh (METHODOLOGY §3)")
    add("E02", s["scope2_market_total"], "inventory 2025 scope2_market; GoO from goo_registry_statement.csv (authoritative), vintage = consumption year; residual mix FI 0.406, SE 0.035")
    x = inv[(inv.scope == "scope2_location") & (inv.line_item == "S-KAN")]
    add("E03", x.tco2e.sum(), "inventory 2025 scope2_location line items S-KAN (electricity x 0.026 + heat x national average); energy:S-KAN:2025-*")
    x = inv[(inv.category == "cat01_purchased_goods") & inv.line_item.str.match(r"M10[2-7]:")]
    add("E04", x.tco2e.sum(), "cat01 lines M102-M107 (EE, LV, LT, SE, DK, PL imports); supplier PCFs where valid else EF-RAP-*; erp_purchase_lines (corrected)")
    cat1 = inv[inv.category.isin(["cat01_purchased_goods", "cat01_purchased_services"])].tco2e.sum()
    rap = inv[(inv.category == "cat01_purchased_goods") & (inv.line_item.str.match(r"M10[0-7]:") | (inv.line_item == "contract_farming:FI_SW:turnip_rape"))].tco2e.sum()
    add("E05", 100 * rap / cat1, "cat01 rapeseed lines M101-M107 + contract_farming:FI_SW:turnip_rape over all cat01 (goods + services)")
    tot = s["scope1_total"] + s["scope2_market_total"] + s["scope3_total"]
    add("E06", 100 * fl.flag_tco2e.sum() / tot, "FLAG = factor flag_share x cat01 goods + contract-farm N2O, liming and organic soils (METHODOLOGY §5) over S1 + S2(MB) + S3")
    add("E07", 100 * fd25.organic_tco2e.sum() / fd25.tco2e.sum(), "farm_detail 2025: organic-soil share (survey or soil map, whichever higher) x 29 t CO2 + 13 kg N2O-N per ha")
    ss = __import__("juuri.quality", fromlist=["x"]).soil_share(raw())
    f25 = ss[(ss.year == 2025) & ss.crop.isin(C.APETIT_CROPS) & ss.farm_id.str.startswith("F")].groupby("farm_id")[["area_ha", "org_ha"]].sum()
    rep = T["_tables"]["farm_survey"]
    rep_org = set(rep[(rep.year == 2025) & rep.soil_type_reported.str.startswith("Organic")].farm_id)
    n = int(((f25.org_ha / f25.area_ha >= 0.3) | f25.index.isin(rep_org)).sum())
    add("E08", n, "parcels.csv soil_class_soilmap (≈88 % accurate) x parcel_crop_history 2025 declared contract crops, plus farms reporting organic soil")
    bio = T["energy"][(T["energy"].year == 2025) & (T["energy"].site_id == "S-KAN")]
    add("E09", sum(g.mwh.sum() * F.FUEL_BIOGENIC_T_PER_MWH.get(c, 0) for c, g in bio.groupby("carrier")),
        "energy:S-KAN:2025-*:wood_chips / oilseed_sidestream (+ diesel bio share) x biogenic factors 0.4032 / 0.45 t/MWh")
    el = T["energy"][(T["energy"].year == 2025) & (T["energy"].site_id == "S-SAK") & (T["energy"].carrier == "electricity")]
    add("E10", el.mwh.sum(), "energy:S-SAK:2025-MM:electricity after approved meter corrections (see audit log)")
    sak = T["energy"][(T["energy"].year == 2025) & (T["energy"].site_id == "S-SAK")]
    sak = sak[sak.carrier != "district_heat"]
    goo = T["goo"]
    g = goo[(goo.year == 2025) & (goo.site_id == "S-SAK") & (goo.vintage_year == 2025)].mwh_cancelled.sum()
    elec = sak[sak.carrier == "electricity"].mwh.sum()
    ren = sak[sak.carrier.isin(F.FUEL_RENEWABLE)].mwh.sum() + min(g, elec)
    add("E11", 100 * ren / sak.mwh.sum(), "S-SAK 2025 fuels (biogas renewable; LNG/LPG fossil) + electricity; GoO-backed MWh from registry counted renewable")
    w = raw()["water"]
    w24 = w[w.year == 2024]
    wd = w24[(w24.flow == "withdrawal") & (w24.source_or_destination != "reused_water")].m3.sum()
    dis = w24[w24.flow == "discharge"].m3.sum()
    add("E12", wd - dis, f"water_balance.csv 2024: withdrawals excl. reused_water ({wd:,} m3) - discharges ({dis:,} m3), GRI 303-5")
    x = inv[(inv.category == "fugitive_refrigerants") & inv.line_item.str.startswith("S-SAK")]
    add("E13", x.tco2e.sum(), "refrigerant_service_log 2025 S-SAK (corrected gas codes and units) x AR6 GWP100")
    x = inv[inv.line_item.isin(["inbound:EF-TKM-SHIP-BULK", "inbound:EF-TKM-SHIP-REEFER"])]
    add("E14", x.tco2e.sum(), "shipments.csv inbound legs EF-TKM-SHIP-BULK + EF-TKM-SHIP-REEFER (incl. imputed missing legs) x WTW EF")
    pur = T["purchases"]
    add("E15", pur[(pur.year == 2025) & (pur.material_id == "M130")].qty_kg.sum() / 1000, "erp_purchase_lines M130 2025, receipts net of returns (movement 122)")
    for q, crop in [("E16", "pea"), ("E17", "turnip_rape")]:
        g = fd25[fd25.crop == crop]
        add(q, g.tco2e.sum() * 1000 / g.delivered_kg.sum(), f"farm_detail 2025 FI {crop}: Σ farm emissions / Σ delivered kg (ERP {'M110' if crop == 'pea' else 'M100'}), organic soils included")
    sv = T["_tables"]["farm_survey"]
    pea = sv[(sv.crop == "pea") & sv.year.isin([2025, 2026])].assign(t=lambda d: d.area_ha * d.yield_t_ha)
    both = set(pea[pea.year == 2025].farm_id) & set(pea[pea.year == 2026].farm_id)
    pv = pea[pea.farm_id.isin(both)].groupby("year").t.sum()
    add("E18", 100 * (pv[2026] / pv[2025] - 1), f"farm_survey_fi.csv pea, {len(both)} growers reporting in both 2025 and 2026 (area x yield; ERP has no 2026 deliveries)")
    fh = raw()["foodhills"].groupby("skördeår").levererad_mängd_ton.sum()
    add("E19", 100 * (fh[2026] / fh[2025] - 1), "foodhills_odlingsdata_se.csv levererad_mängd_ton 2026 vs 2025")
    s12 = s["scope1_total"] + s["scope2_market_total"]
    add("E20", s12 * (1 - 0.042 * 7), f"2025 S1 + S2(MB) = {s12:,.0f} t x (1 - 4.2 % x 7 yr); framework_digest.md 'Minimum ambition'")
    add("E21", fl.flag_tco2e.sum() * (1 - 0.0303 * 7), f"2025 FLAG = {fl.flag_tco2e.sum():,.0f} t x (1 - 3.03 % x 7 yr); framework_digest.md 'Minimum ambition'")
    for q, a, ev in RUBRIC:
        add(q, a, ev)
    return pd.DataFrame(A)


RUBRIC = [
    ("E30", "Yes. SBTi FLAG guidance makes a FLAG target mandatory for food production and food & beverage processing companies, and for any company whose FLAG emissions exceed 20 % of the total. Apetit is a food processor, and in the reviewed 2025 inventory FLAG emissions are well above 20 % of the total (see E06). It also needs a no-deforestation commitment (cut-off no later than 2020).",
     "data/07_frameworks/framework_digest.md - Climate targets: SBTi - FLAG; SBTi FLAG guidance (source_registry.csv)"),
    ("E31", "(a) Scope 1+2: at least 4.2 % per year linear (1.5 °C); (b) Scope 3: at least 2.5 % per year linear (well-below 2 °C); (c) FLAG: at least 3.03 % per year linear.",
     "data/07_frameworks/framework_digest.md - Minimum ambition (V1.3.1 method appendix); SBTi CNZS V1.3.1 Method Appendix"),
    ("E32", "A 2019 base year must be recalculated before use: remove the divested grain trade (Scope 3 cat 1, divested 10 Mar / 31 May 2022) and add Foodhills (acquired 27 Nov 2025: Bjuv operations and ~300 Swedish pea growers) as if they had always been in the boundary - both exceed the 5 % significance threshold. Because 2019 was also calculated with older methods (no organic soils, no PCFs), SBTi V2's preference for the most recent year with comprehensive data points to a structurally adjusted 2025 base instead.",
     "structural_events.csv rows 2022-03-10, 2022-05-31, 2025-11-27; GHG Protocol Corporate Standard ch. 5 (base-year recalculation); docs/METHODOLOGY.md §1; framework_digest.md - SBTi V2 base year"),
    ("E33", "No. Location-based Scope 2 uses the grid-average factor regardless of contractual instruments. GoOs only reduce market-based Scope 2 (and only for the consumption year that matches their production period).",
     "GHG Protocol Scope 2 Guidance (dual reporting); framework_digest.md - Accounting: Scope 2; docs/METHODOLOGY.md §3"),
    ("E34", "FLAG: direct and indirect field N2O (mineral and manure N, crop residues), drained organic soils (CO2 and N2O), liming, land-use change and soil carbon. Non-FLAG: fertiliser manufacture, farm diesel, electricity for storage/drying/irrigation, pesticides (and processing).",
     "docs/METHODOLOGY.md §5 'FLAG (SBTi) part'; framework_digest.md - SBTi FLAG"),
    ("E35", "Rapeseed is not an EUDR relevant commodity. EUDR covers cattle, cocoa, coffee, palm oil, rubber, soya and wood, but scope is set by HS codes in Annex I: soy protein isolate (HS 3504) is not listed, so it is out of scope as an HS code - although it is derived from soy, an SBTN high-impact commodity, and should still be screened for deforestation risk. Screen HS codes, as Annex I can be amended.",
     "framework_digest.md - Regulation: EU Deforestation Regulation; structural_events.csv 2026-12-30; materials.csv M130 eudr_commodity=False, sbtn_high_impact_commodity=True"),
    ("E36", "Revised ESRS E3 defines an area of water stress as one where Aqueduct baseline water stress is 'high' or above, or where WEI+ is at least 40 %.",
     "framework_digest.md - Nature: ESRS (revised, 2026) E3 water"),
    ("E37", "Yes, but optionally. Under the GHG Protocol Land Sector and Removals Standard (effective 1 Jan 2027) removals may be reported only if they are monitored, traceable and permanence/reversals are accounted for. Catch-crop soil carbon therefore needs monitoring (e.g. soil sampling) before it is reported, and it must be reported separately from emissions, never netted into reductions.",
     "framework_digest.md - Accounting: Land Sector and Removals Standard; decarbonisation_levers.csv L10 trade_offs"),
    ("E38", "SBTN Step 1a (materiality screening) uses ENCORE-based pressures. Pressure categories: land/freshwater/sea use change, water use, pollution (nutrients, pesticides, other), GHG emissions, and other pressures such as resource exploitation and invasive species. In this solution they are screened as land use, water use, nutrient pollution, GHG and pesticides.",
     "framework_digest.md - SBTN steps; docs/NATURE_SBTN.md - Mapping to SBTN steps (1a)"),
    ("E39", "Because the atmosphere sees the same emissions: replacing a spend-based or average factor with a supplier-specific one changes the estimate, not the physical emissions. It is a methodology change (lever L27 'measurement improvement, not abatement'), so the base year must be recalculated with the better data if the change is significant, and the difference must not be presented as progress against the target.",
     "decarbonisation_levers.csv L27; GHG Protocol Corporate Standard base-year recalculation for methodology changes; docs/TASKS_AND_SCORING.md B1/B2"),
]


# ------------------------------------------------------------------------------ Claude agent
def _df(d: pd.DataFrame, n: int = 40) -> str:
    return d.head(n).to_csv(index=False)


def _tool_impl(name: str, args: dict) -> str:
    T = INV.canonical()
    if name == "get_inventory_summary":
        return _df(INV.summary(T, int(args.get("year", 2025))))
    if name == "get_line_items":
        inv = INV.line_items(T, int(args.get("year", 2025)))
        if args.get("category"):
            inv = inv[inv.category.str.contains(args["category"])]
        return _df(inv.sort_values("tco2e", ascending=False), 60)
    if name == "get_lineage":
        lin = INV.lineage(T, int(args.get("year", 2025)))
        lin = lin[lin.line_item == args["line_item"]]
        return f"{len(lin)} source records, top by tCO2e:\n" + _df(lin.sort_values("tco2e", ascending=False), 25)
    if name == "get_data_issues":
        iss = T["_issues"]
        if args.get("table"):
            iss = iss[iss.table == args["table"]]
        if args.get("year"):
            iss = iss[iss.year == int(args["year"])]
        if args.get("record_key"):
            iss = iss[iss.record_key == args["record_key"]]
        return f"{len(iss)} issues\n" + _df(iss[["record_key", "issue_type", "field", "old_value", "new_value", "status", "applied_as", "explanation"]], 40)
    if name == "get_answer":
        a = answer_e()
        return _df(a[a.question_id == args["question_id"]])
    if name == "search_docs":
        return json.dumps(search_docs(args["query"]), ensure_ascii=False)
    if name == "get_pathway":
        from . import pathways as P
        b = P.base_inventory()
        pw = P.pathway(b, args.get("scenario", "net_zero_2050"))
        return _df(pw.round(0)) + "\nTargets:\n" + _df(P.targets(b).round(1))
    if name == "get_climate_risk":
        from . import climate as CL
        return _df(CL.c2_risk().round(3)) + "\nEUR risk:\n" + _df(CL.c3_euro_risk().round(0))
    if name == "query_context_broker":
        from . import fiware as FW
        b = FW.Broker()
        if not b.alive():
            return "The live context broker (FIWARE Orion-LD) is not running; use the batch tools instead."
        near = (args["near_lat"], args["near_lon"]) if args.get("near_lat") is not None and args.get("near_lon") is not None else None
        res = b.query(args["entity_type"], q=args.get("q"), attrs=args.get("attrs"), near=near,
                      max_distance_m=args.get("max_distance_m"), limit=int(args.get("limit", 50)))
        for e in res:   # geometry is bulky and rarely needed for an answer
            if isinstance(e.get("location"), dict) and e["location"].get("type") != "Point":
                e["location"] = f"{e['location'].get('type')} (omitted)"
        return f"{len(res)} entities from {FW.NGSI} (live context, NGSI-LD keyValues):\n" + json.dumps(res, ensure_ascii=False, default=str)[:20000]
    if name == "get_catchment_priority":
        from . import nature as N
        return _df(N.d1_priority().round(2), 42)
    return f"unknown tool {name}"


TOOLS = [
    dict(name="get_inventory_summary", description="GHG inventory totals by scope and category for a year (2021-2025), tCO2e, from the reviewed silver data.",
         input_schema=dict(type="object", properties=dict(year=dict(type="integer")), required=["year"], additionalProperties=False)),
    dict(name="get_line_items", description="Inventory line items (site:fuel, material:supplier, contract_farming:region:crop ...) with basis, optionally filtered by a category substring.",
         input_schema=dict(type="object", properties=dict(year=dict(type="integer"), category=dict(type="string")), required=["year"], additionalProperties=False)),
    dict(name="get_lineage", description="Source records (record keys, activity, factor, factor source) behind one inventory line item. Use to cite evidence.",
         input_schema=dict(type="object", properties=dict(year=dict(type="integer"), line_item=dict(type="string")), required=["year", "line_item"], additionalProperties=False)),
    dict(name="get_data_issues", description="Data-quality issues and corrections from the audit log, with review status.",
         input_schema=dict(type="object", properties=dict(table=dict(type="string"), year=dict(type="integer"), record_key=dict(type="string")), additionalProperties=False)),
    dict(name="get_answer", description="Pre-computed, evidence-backed answer for a challenge question id (E01-E39).",
         input_schema=dict(type="object", properties=dict(question_id=dict(type="string")), required=["question_id"], additionalProperties=False)),
    dict(name="search_docs", description="Search the framework digest (SBTi, GHG Protocol, SBTN, ESRS, EUDR, NGFS) and methodology docs. Returns file and section to cite.",
         input_schema=dict(type="object", properties=dict(query=dict(type="string")), required=["query"], additionalProperties=False)),
    dict(name="get_pathway", description="SBTi targets and yearly BAU vs with-levers pathway under an NGFS-style scenario (net_zero_2050, below_2c, delayed_transition, current_policies, ndcs, fragmented_world, low_demand).",
         input_schema=dict(type="object", properties=dict(scenario=dict(type="string")), additionalProperties=False)),
    dict(name="get_climate_risk", description="2041-2060 yield change and bad-year probability by crop/region/SSP, and EUR shortfall risk.",
         input_schema=dict(type="object", properties={}, additionalProperties=False)),
    dict(name="query_context_broker",
         description="Query LIVE context in the FIWARE Orion-LD broker (NGSI-LD, Smart Data Models). Entity types: AgriParcel "
                     "(area, hasAgriCrop, belongsTo, refCatchment, phosphorusToWater, droughtAlert none|moderate|high, "
                     "expectedYieldImpact %, waterDeficitToDate mm), AgriFarm (farmEmissions t, organicSoilShare), Catchment "
                     "(priorityRank, ecologicalStatus, phosphorusToWater kg), WeatherObserved (dateObserved, temperature, precipitation), "
                     "DeviceMeasurement (numValue MWh, dateObserved), DataQualityIssue (issueType, recordKey, reviewStatus), Building, AgriCrop. "
                     "q uses NGSI-LD query language, e.g. droughtAlert==\"high\";refCatchment==\"urn:ngsi-ld:Catchment:CA12\" or priorityRank<6. "
                     "Cite entity ids.",
         input_schema=dict(type="object", properties=dict(
             entity_type=dict(type="string"), q=dict(type="string"), attrs=dict(type="string", description="comma-separated"),
             near_lat=dict(type="number"), near_lon=dict(type="number"), max_distance_m=dict(type="number"), limit=dict(type="integer")),
             required=["entity_type"], additionalProperties=False)),
    dict(name="get_catchment_priority", description="SBTN-style catchment priority: Apetit P and N to water, state, sensitivity, rank.",
         input_schema=dict(type="object", properties={}, additionalProperties=False)),
]

SYSTEM = """You are Juuri, the climate & nature copilot for Apetit's sustainability team, management and farming advisors.
Answer from the tools only. Every number you give must come from a tool result, and you cite where it came from:
record keys, the file and line item, or the framework document and section. If the tools do not answer the
question, say what data is missing rather than estimating. Results marked 'provisional' have not yet been approved
by a reviewer; say so when it matters. Never present measurement or structural changes as emission reductions.
The data is a synthetic twin of Apetit, so do not present it as Apetit's actual emissions.
Write for non-specialists: short, plain language, the decision-relevant figure first.
For anything happening now (this season's weather, drought alerts, new meter readings, live issues) use
query_context_broker and cite the NGSI-LD entity ids."""


class Copilot:
    def __init__(self):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.messages: list = []

    def ask(self, question: str, max_steps: int = 8) -> tuple[str, list[dict]]:
        """Returns the answer text and a trace of tool calls (name, input, result excerpt)."""
        self.messages.append({"role": "user", "content": question})
        trace = []
        for _ in range(max_steps):
            resp = self.client.beta.messages.create(
                model=MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=self.messages,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                return "The model declined this request.", trace
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                text = "\n".join(b.text for b in resp.content if b.type == "text")
                return text, trace
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                try:
                    out = _tool_impl(b.name, dict(b.input))
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:20000]})
                except Exception as e:  # noqa: BLE001 - surface tool errors to the model
                    out = f"Error: {e}"
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": True})
                trace.append(dict(tool=b.name, input=dict(b.input), result=out[:600]))
            self.messages.append({"role": "user", "content": results})
        return "Stopped after too many tool steps.", trace


def offline_answer(question: str) -> tuple[str, list[dict]]:
    """No-LLM fallback: match a known question, else return the best document passages."""
    a = answer_e()
    qs = pd.read_csv(C.TASKS / "E_questions.csv")
    qs = qs[qs.question_id.isin(a.question_id)]   # dev questions (E90-E92) have no stored answer
    qt = set(_tok(question))
    best, score = None, 0.0
    for _, q in qs.iterrows():
        s = len(qt & set(_tok(q.question))) / max(len(set(_tok(q.question))), 1)
        if s > score:
            best, score = q, s
    hits = search_docs(question, 3)
    if best is not None and score >= 0.45:
        r = a[a.question_id == best.question_id].iloc[0]
        unit = f" {best.unit}" if isinstance(best.unit, str) else ""
        return f"**{best.question_id}: {best.question}**\n\n{r.answer}{unit if best.kind == 'numeric' else ''}\n\n*Evidence:* {r.evidence}", hits
    if hits:
        return "No computed answer matches this question. Relevant passages:\n\n" + "\n\n".join(
            f"> {h['text'][:500]}\n\n— `{h['source']}` · {h['section']}" for h in hits), hits
    return "No matching data or document passage found.", []


def e_submission() -> pd.DataFrame:
    a = answer_e()
    tpl = pd.read_csv(C.TASKS / "templates" / "E_answers_submission.csv")
    return tpl[["question_id"]].merge(a, on="question_id", how="left")
