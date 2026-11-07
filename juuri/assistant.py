"""Conversational assistant: answers questions about the data and produces reports, in en / fi / sv.

* With an Anthropic credential: a Claude agent (tool use) whose tools are Juuri's own data
  functions, the live context broker, and two report tools (standard report, custom report).
* Without one: a deterministic, multilingual intent router over the same data and report engine,
  so the product is fully usable offline.
"""
from __future__ import annotations

import json
import os
import re
import uuid

import pandas as pd

from . import copilot as CP
from . import i18n as I
from . import reports as R

CONVERSATIONS: dict[str, dict] = {}

REPORT_TOOLS = [
    dict(name="make_standard_report",
         description="Generate one of Juuri's standard reports from the reviewed data and return its id and title. kinds: executive, "
                     "inventory (params: year 2021-2025), quality (year), targets (scenario, target_year), climate, nature, "
                     "farm (farm_id e.g. F0123), catchment (catchment_id e.g. CA12). Use when the user asks for a report on one of these.",
         input_schema=dict(type="object", properties=dict(
             kind=dict(type="string", enum=list(R.STANDARD)), year=dict(type="integer"), scenario=dict(type="string"),
             target_year=dict(type="integer"), farm_id=dict(type="string"), catchment_id=dict(type="string")),
             required=["kind"], additionalProperties=False)),
    dict(name="create_custom_report",
         description="Create a custom report answering the user's specific query, built ONLY from figures returned by other tools. "
                     "Each section has a heading, Markdown text, and optionally a table {columns, rows} and/or a chart "
                     "{kind: bar|line, labels, series:[{name, values}]}. Write all text in the user's language. List sources.",
         input_schema=dict(type="object", properties=dict(
             title=dict(type="string"),
             sections=dict(type="array", items=dict(type="object", properties=dict(
                 heading=dict(type="string"), text=dict(type="string"),
                 table=dict(type="object", properties=dict(columns=dict(type="array", items=dict(type="string")),
                                                           rows=dict(type="array", items=dict(type="array")))),
                 chart=dict(type="object", properties=dict(kind=dict(type="string", enum=["bar", "line"]),
                                                           labels=dict(type="array", items=dict(type="string")),
                                                           series=dict(type="array", items=dict(type="object", properties=dict(
                                                               name=dict(type="string"), values=dict(type="array", items=dict(type="number")))))))),
                 required=["heading"])),
             sources=dict(type="array", items=dict(type="string"))),
             required=["title", "sections"], additionalProperties=False)),
]


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def chat(message: str, lang: str = "en", conversation_id: str | None = None) -> dict:
    lang = lang if lang in I.LANGS else "en"
    cid = conversation_id if conversation_id in CONVERSATIONS else uuid.uuid4().hex[:12]
    conv = CONVERSATIONS.setdefault(cid, dict(messages=[], lang=lang))
    if has_credentials():
        try:
            out = _claude(conv, message, lang)
            out["mode"] = "claude"
        except Exception as e:  # noqa: BLE001 - degrade to offline, but say so
            out = offline(message, lang)
            out["text"] = f"_(Claude unavailable: {e.__class__.__name__}; offline answer)_\n\n" + out["text"]
            out["mode"] = "offline"
    else:
        out = offline(message, lang)
        out["mode"] = "offline"
    out["conversation_id"] = cid
    return out


# ------------------------------------------------------------------------------ Claude agent
def _system(lang: str) -> str:
    return (CP.SYSTEM + f"\n\nAlways reply in {I.LANG_NAME[lang]}, whatever language the data or documents are in. "
            "When the user asks for a report, a summary to share, or a document, produce one with make_standard_report "
            "(if a standard report fits) or create_custom_report (built only from tool results), then tell the user its "
            "title in one sentence plus the 2-3 key findings. Number formats: use the conventions of the reply language.")


def _claude(conv: dict, message: str, lang: str, max_steps: int = 10) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    msgs = conv["messages"]
    msgs.append({"role": "user", "content": message})
    trace, made = [], []
    for _ in range(max_steps):
        resp = client.beta.messages.create(
            model=CP.MODEL, max_tokens=16000, system=_system(lang), tools=CP.TOOLS + REPORT_TOOLS, messages=msgs,
            output_config={"effort": "medium"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if resp.stop_reason == "refusal":
            return dict(text="The model declined this request.", trace=trace, reports=made)
        msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return dict(text="\n".join(b.text for b in resp.content if b.type == "text"), trace=trace, reports=made)
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            args = dict(b.input)
            try:
                if b.name == "make_standard_report":
                    kind = args.pop("kind")
                    rep = R.standard(kind, lang, **args)
                    made.append(dict(id=rep["id"], title=rep["title"]))
                    out = json.dumps(dict(report_id=rep["id"], title=rep["title"], url=f"/api/reports/{rep['id']}.html",
                                          sections=[s.get("heading") for s in rep["sections"]]), ensure_ascii=False)
                elif b.name == "create_custom_report":
                    rep = R.custom(lang, args["title"], args["sections"], args.get("sources"))
                    made.append(dict(id=rep["id"], title=rep["title"]))
                    out = json.dumps(dict(report_id=rep["id"], url=f"/api/reports/{rep['id']}.html"), ensure_ascii=False)
                else:
                    out = CP._tool_impl(b.name, args)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:20000]})
            except Exception as e:  # noqa: BLE001 - report tool errors back to the model
                out = f"Error: {e}"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": True})
            trace.append(dict(tool=b.name, input=dict(b.input), result=out[:600]))
        msgs.append({"role": "user", "content": results})
    return dict(text="Stopped after too many tool steps.", trace=trace, reports=made)


# ------------------------------------------------------------------------------ offline router
KW = {
    "report": r"report|raport|rapport|yhteenveto|sammanfatt|summary|dokument|document",
    "executive": r"executive|johdon|ledning|overall|kokonais|helhet",
    "climate": r"climat|ilmasto|klimat|drought|kuivuu|tork|yield|sato|skörd|ssp",
    "nature": r"natur|luonto|catchment|valuma|avrinning|phosph|fosfor|sbtn|eutro|rehevö|övergöd|biodiv",
    "targets": r"target|tavoit|mål|pathway|polku|färdväg|sbti|macc|lever|toimenpi|åtgärd|abatement|vähennys",
    "quality": r"quality|laatu|kvalitet|issue|ongelm|problem|correct|korja|korriger|review|tarkast|gransk|audit",
    "inventory": r"inventor|inventaar|invente|emission|päästö|utsläpp|scope|ghg|kasvihuone|växthus|footprint|jalanjälj|fotavtryck",
    "flag": r"\bflag\b",
    "peat": r"peat|turve|turpee|torv|organic soil|eloper|organogen",
    "water": r"water consumption|vedenkulutus|vattenförbrukning|water use|vesi|vatten",
    "where_first": r"first|ensin|först|priorit|which catchment|mille valuma|vilket avrinning",
}


def _has(text, key):
    return re.search(KW[key], text, re.I) is not None


def offline(message: str, lang: str) -> dict:
    from . import inventory as INV, nature as N, pathways as P
    q = message.strip()
    farm = re.search(r"\b([FS]\d{4})\b", q, re.I)
    catch = re.search(r"\b(C[AS]\d{2})\b", q, re.I)
    year_m = re.search(r"\b(202[1-5])\b", q)
    year = int(year_m.group(1)) if year_m else 2025
    wants_report = _has(q, "report")

    def made(rep):
        summary = next((sec.get("text") for sec in rep["sections"] if sec.get("text")), "")
        heads = [sec["heading"] for sec in rep["sections"][1:] if sec.get("heading")]
        text = I.t(lang, "chat.report_made", title=rep["title"]) + ("\n\n" + summary if summary else "") + \
            ("\n\n" + "\n".join(f"- {h}" for h in heads) if heads else "")
        return dict(text=text, reports=[dict(id=rep["id"], title=rep["title"])],
                    trace=[dict(tool="make_standard_report", input=dict(kind=rep["kind"], **rep["params"]))])

    try:
        if farm:
            return made(R.standard("farm", lang, farm_id=farm.group(1).upper()))
        if catch:
            return made(R.standard("catchment", lang, catchment_id=catch.group(1).upper()))
    except KeyError as e:
        return dict(text=str(e).strip("'\""), reports=[], trace=[])
    if wants_report:
        for kind in ["executive", "climate", "nature", "targets", "quality", "inventory"]:
            if _has(q, kind):
                return made(R.standard(kind, lang, year=year))
        return made(R.standard("executive", lang))

    e_text, e_hits = CP.offline_answer(q)
    if e_text.startswith("**E"):
        return dict(text=e_text, reports=[], trace=[dict(tool="get_answer")])
    T = INV.canonical()
    s = INV.summary(T, year).set_index("category").tco2e
    fl = INV.flag_split(T, year)
    tot = s["scope1_total"] + s["scope2_market_total"] + s["scope3_total"]
    ev = f"inventory {year} (reviewed silver data) · outputs/lineage_{year}.csv"
    if _has(q, "flag"):
        share = 100 * fl.flag_tco2e.sum() / tot
        txt = (f"**FLAG {year}: {I.tonnes(lang, fl.flag_tco2e.sum())} = {I.pct(lang, share)}** "
               f"({I.t(lang, 'kpi.flag_share').lower()}).\n\n{I.t(lang, 'report.rec.flag')}")
        return dict(text=txt, reports=[], trace=[dict(tool="flag_split", evidence=ev + "; framework_digest.md SBTi FLAG")])
    if _has(q, "peat"):
        fd = T["farm_detail"]
        fd = fd[(fd.year == year) & (fd.region == "FI_SW")]
        txt = I.t(lang, "report.inv.peat", share=I.pct(lang, 100 * fd.organic_tco2e.sum() / fd.tco2e.sum())) + "\n\n" + I.t(lang, "report.rec.peat")
        return dict(text=txt, reports=[], trace=[dict(tool="farm_detail", evidence="parcels.csv soil map × farm survey (METHODOLOGY §5)")])
    if _has(q, "where_first") or (_has(q, "nature") and not _has(q, "inventory")):
        pr = N.d1_priority().head(3)
        lines = [f"{int(r.priority_rank)}. **{r.catchment_id}** — {R.STATUS.get(r.ecological_status_class, {}).get(lang, r.ecological_status_class)}, "
                 f"{I.num(lang, r.apetit_p_to_water_kg)} kg P, {I.num(lang, r.apetit_n_to_water_kg)} kg N" for r in pr.itertuples()]
        hs = N.parcel_hotspots(3)
        lines.append("")
        lines += [f"- {h.parcel_id} ({I.label(lang, 'crop', h.crop)}, {h.catchment_id}): {R.action(lang, h.action)}" for h in hs.itertuples()]
        return dict(text=I.t(lang, "report.n.intro") + "\n\n" + "\n".join(lines) + "\n\n" + I.t(lang, "report.rec.nature"), reports=[],
                    trace=[dict(tool="get_catchment_priority", evidence="D1 priority = (P/median + 0.5 N/median) × state × sensitivity")])
    if _has(q, "climate"):
        from . import climate as CL
        e = CL.c3_euro_risk()
        e = e[e.scenario == "ssp585"].sort_values("expected_cost_eur", ascending=False).iloc[0]
        r = CL.c2_risk()
        r = r[r.scenario == "ssp585"].sort_values("mean_yield_change_pct")
        lines = [f"- {I.label(lang, 'crop', x.crop)} ({R.region(lang, x.region)}): {I.pct(lang, x.mean_yield_change_pct, 1)}" for x in r.itertuples()]
        return dict(text=I.t(lang, "report.c.worst", crop=I.label(lang, "crop", e.crop), region=R.region(lang, e.region),
                                 eur=I.eur(lang, e.expected_cost_eur), p95=I.eur(lang, e.p95_cost_eur)) + "\n\nSSP5-8.5, 2041–2060:\n" + "\n".join(lines),
                    reports=[], trace=[dict(tool="get_climate_risk", evidence="C2/C3 · outputs/C3_euro_risk.csv")])
    if _has(q, "targets"):
        b = P.base_inventory()
        tg = P.targets(b)
        lines = [f"- {I.t(lang, f'boundary.{x.boundary}')}: {I.tonnes(lang, x.base_tco2e)} → **{I.tonnes(lang, x.target_tco2e)}** (2032, −{I.pct(lang, x.reduction_pct, 1)})"
                 for x in tg.itertuples()]
        m = P.macc(b).head(3)
        lines += [""] + [f"- {x.lever_id} {I.lever_name(lang, x.lever_id, x.name)}: {I.num(lang, x.abatement_tco2e)} t, {I.eur(lang, x.cost_eur_per_t_scenario)}/t" for x in m.itertuples()]
        return dict(text=I.t(lang, "report.t.intro", base=2025, ty=2032) + "\n\n" + "\n".join(lines), reports=[],
                    trace=[dict(tool="get_pathway", evidence="SBTi rates; levers recomputed from inventory")])
    if _has(q, "quality"):
        iss = T["_issues"]
        x = iss[iss.year == year]
        g = x.groupby("issue_type").size().sort_values(ascending=False).head(6)
        return dict(text=I.t(lang, "report.q.intro", n=len(x), year=year, applied=int(x.applied.sum()), approved=int((x.status == "approved").sum()),
                             rejected=int((x.status == "rejected").sum())) + "\n\n" + "\n".join(f"- {I.label(lang, 'issue', k)}: {v}" for k, v in g.items()),
                    reports=[], trace=[dict(tool="get_data_issues", evidence="outputs/issues_all_years.csv")])
    if _has(q, "inventory"):
        return dict(text=I.t(lang, "report.inv.intro", year=year, s1=I.tonnes(lang, s["scope1_total"]), s2m=I.tonnes(lang, s["scope2_market_total"]),
                             s3=I.tonnes(lang, s["scope3_total"]), flag=I.pct(lang, 100 * fl.flag_tco2e.sum() / tot)), reports=[],
                    trace=[dict(tool="get_inventory_summary", evidence=ev)])
    hits = e_hits
    if hits:
        return dict(text=I.t(lang, "chat.offline_fallback") + "\n\n" + "\n\n".join(
            f"> {h['text'][:400]}\n\n— `{h['source']}` · {h['section']}" for h in hits[:3]), reports=[], trace=[dict(tool="search_docs")])
    return dict(text=I.t(lang, "chat.offline_nothing"), reports=[], trace=[])
