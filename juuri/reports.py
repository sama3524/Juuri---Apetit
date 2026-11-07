"""Report engine: standard reports built from the reviewed data, plus custom reports composed by the
assistant. A report is a JSON document (title, sections with Markdown text, tables and charts,
sources) that renders to a printable HTML page (inline SVG charts, no external assets) or Markdown.
All text is localised (en / fi / sv)."""
from __future__ import annotations

import datetime as dt
import html
import json
import uuid

import markdown as md
import numpy as np
import pandas as pd

from . import config as C
from . import i18n as I

REPORT_DIR = C.OUTPUTS / "reports"

COLS = {
    "en": dict(category="Category", scope="Scope", tco2e="t CO2e", share="Share", line_item="Line item", basis="Method",
               issues="Issues", type="Type", lever="Lever", abatement="Abatement t/yr", cost="€ per t", owner="Owner",
               boundary="Boundary", base="Base", target="Target", reduction="Reduction", year="Year", bau="BAU", net="With levers",
               change="Yield change", p_bad="P(bad year)", shortfall="Shortfall t/yr", cost_k="Expected k€/yr", p95_k="P95 k€/yr",
               catchment="Catchment", status="Status", p="P kg/yr", n="N kg/yr", rank="Rank", parcel="Parcel", area="Area ha",
               action="Action", material="Material", origin="Origin", score="Materiality", why="Why", crop="Crop", region="Region",
               scenario="Scenario", record="Record", proposed="Proposed", confidence="Confidence", soil="Soil", drought="Drought alert"),
    "fi": dict(category="Luokka", scope="Scope", tco2e="t CO2e", share="Osuus", line_item="Rivi", basis="Menetelmä",
               issues="Ongelmat", type="Tyyppi", lever="Toimenpide", abatement="Vähennys t/v", cost="€ / t", owner="Vastuu",
               boundary="Rajaus", base="Lähtötaso", target="Tavoite", reduction="Vähennys", year="Vuosi", bau="Nykymeno", net="Toimenpiteillä",
               change="Sadon muutos", p_bad="P(huono vuosi)", shortfall="Vaje t/v", cost_k="Odotettu k€/v", p95_k="P95 k€/v",
               catchment="Valuma-alue", status="Tila", p="P kg/v", n="N kg/v", rank="Sija", parcel="Lohko", area="Ala ha",
               action="Toimenpide", material="Raaka-aine", origin="Alkuperä", score="Olennaisuus", why="Peruste", crop="Kasvi", region="Alue",
               scenario="Skenaario", record="Tietue", proposed="Ehdotettu", confidence="Luottamus", soil="Maalaji", drought="Kuivuushälytys"),
    "sv": dict(category="Kategori", scope="Scope", tco2e="t CO2e", share="Andel", line_item="Post", basis="Metod",
               issues="Problem", type="Typ", lever="Åtgärd", abatement="Minskning t/år", cost="€ per t", owner="Ansvarig",
               boundary="Avgränsning", base="Basnivå", target="Mål", reduction="Minskning", year="År", bau="Oförändrat", net="Med åtgärder",
               change="Skördeförändring", p_bad="P(dåligt år)", shortfall="Underskott t/år", cost_k="Förväntat k€/år", p95_k="P95 k€/år",
               catchment="Avrinningsområde", status="Status", p="P kg/år", n="N kg/år", rank="Rang", parcel="Skifte", area="Areal ha",
               action="Åtgärd", material="Råvara", origin="Ursprung", score="Väsentlighet", why="Varför", crop="Gröda", region="Region",
               scenario="Scenario", record="Post", proposed="Föreslaget", confidence="Tillförlitlighet", soil="Jordart", drought="Torklarm"),
}
REGION = {"FI_SW": {"en": "south-west Finland", "fi": "Lounais-Suomi", "sv": "sydvästra Finland"},
          "SE_SKANE": {"en": "Skåne, Sweden", "fi": "Skåne, Ruotsi", "sv": "Skåne"}}
WATER = {"lake": {"en": "lake", "fi": "järvi", "sv": "sjö"}, "river": {"en": "river", "fi": "joki", "sv": "å"},
         "coastal": {"en": "coastal water", "fi": "rannikkovesi", "sv": "kustvatten"}}
STATUS = {"good": {"en": "good", "fi": "hyvä", "sv": "god"}, "moderate": {"en": "moderate", "fi": "tyydyttävä", "sv": "måttlig"},
          "poor": {"en": "poor", "fi": "välttävä", "sv": "otillfredsställande"}, "bad": {"en": "bad", "fi": "huono", "sv": "dålig"}}


ACTIONS = {
    "Add buffer strip (L09/L10)": {"fi": "Lisää suojakaista (L09/L10)", "sv": "Anlägg skyddszon (L09/L10)"},
    "Reduce P fertilisation; soil-P is high (L09)": {"fi": "Vähennä fosforilannoitusta; maan P-luku on korkea (L09)",
                                                     "sv": "Minska fosforgödslingen; markens P-tal är högt (L09)"},
    "Catch crop after harvest (L10)": {"fi": "Kerääjäkasvi sadonkorjuun jälkeen (L10)", "sv": "Fånggröda efter skörd (L10)"},
    "Reduced tillage / winter cover on slope": {"fi": "Kevennetty muokkaus / talviaikainen kasvipeite rinteessä",
                                                "sv": "Reducerad bearbetning / vintergrönt på sluttning"},
    "Monitor": {"fi": "Seuraa", "sv": "Följ upp"},
}


def action(lang, a):
    return ACTIONS.get(a, {}).get(lang, a)


def _tr_levers(m: pd.DataFrame, lang: str) -> pd.DataFrame:
    return m.assign(name=[I.lever_name(lang, i, n) for i, n in zip(m.lever_id, m.name)], owner=[I.owner(lang, o) for o in m.owner])


def col(lang, k):
    return COLS.get(lang, COLS["en"]).get(k, k)


def region(lang, r):
    return REGION.get(r, {}).get(lang, r)


# ------------------------------------------------------------------------------ document model
def new_report(kind: str, lang: str, title: str, params: dict | None = None) -> dict:
    from . import inventory as INV
    T = INV.canonical()
    iss = T["_issues"]
    prov = int((iss.applied & (iss.applied_as == "provisional")).sum())
    return dict(id=uuid.uuid4().hex[:10], kind=kind, lang=lang, title=title, params=params or {},
                created=dt.datetime.now().isoformat(timespec="seconds"), provisional=prov, sections=[], sources=[])


def section(rep, heading=None, text=None, table=None, chart=None):
    rep["sections"].append({k: v for k, v in dict(heading=heading, text=text, table=table, chart=chart).items() if v is not None})


def df_table(df: pd.DataFrame, headers: list[str]) -> dict:
    rows = [[None if (isinstance(v, float) and np.isnan(v)) else v for v in r] for r in df.itertuples(index=False)]
    return dict(columns=headers, rows=rows)


def save(rep: dict) -> dict:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{rep['id']}.json").write_text(json.dumps(rep, ensure_ascii=False, default=_jd), encoding="utf-8")
    return rep


def _jd(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def load(rid: str) -> dict:
    p = REPORT_DIR / f"{rid}.json"
    if not p.exists() or "/" in rid or ".." in rid:
        raise FileNotFoundError(rid)
    return json.loads(p.read_text(encoding="utf-8"))


def list_reports() -> list[dict]:
    if not REPORT_DIR.exists():
        return []
    out = []
    for p in REPORT_DIR.glob("*.json"):
        r = json.loads(p.read_text(encoding="utf-8"))
        out.append({k: r[k] for k in ("id", "kind", "lang", "title", "created")})
    return sorted(out, key=lambda r: r["created"], reverse=True)


# ------------------------------------------------------------------------------ rendering
def _svg_hbar(chart: dict, lang: str) -> str:
    labels, vals = chart["labels"], [float(v or 0) for v in chart["series"][0]["values"]]
    w, lh, lw = 720, 24, 250
    h = lh * len(labels) + 20
    mx = max(max(vals), 1e-9)
    mn = min(min(vals), 0)
    span = mx - mn
    x0 = lw + (0 - mn) / span * (w - lw - 90)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">']
    for i, (lab, v) in enumerate(zip(labels, vals)):
        y = 10 + i * lh
        x1 = lw + (v - mn) / span * (w - lw - 90)
        xa, xb = sorted([x0, x1])
        color = "#b23a48" if v < 0 else chart.get("color", "#2f6f4f")
        out.append(f'<text x="{lw - 8}" y="{y + 15}" text-anchor="end" font-size="12" fill="#333">{html.escape(str(lab))[:38]}</text>')
        out.append(f'<rect x="{xa:.1f}" y="{y + 3}" width="{max(xb - xa, 1):.1f}" height="{lh - 8}" fill="{color}" rx="2"/>')
        out.append(f'<text x="{xb + 6:.1f}" y="{y + 15}" font-size="11" fill="#555">{I.num(lang, v, chart.get("digits", 0))}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_line(chart: dict, lang: str) -> str:
    x = chart["labels"]
    series = chart["series"]
    w, h, pl, pb = 720, 300, 70, 40
    allv = [float(v) for s in series for v in s["values"] if v is not None]
    lo, hi = min(min(allv), 0), max(allv) * 1.05
    colors = ["#2f6f4f", "#9aa5ad", "#b23a48", "#c9a227", "#5b7f95"]

    def px(i):
        return pl + i / max(len(x) - 1, 1) * (w - pl - 20)

    def py(v):
        return h - pb - (v - lo) / (hi - lo or 1) * (h - pb - 20)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        out.append(f'<line x1="{pl}" x2="{w - 20}" y1="{py(v):.1f}" y2="{py(v):.1f}" stroke="#e5e7eb"/>'
                   f'<text x="{pl - 6}" y="{py(v) + 4:.1f}" font-size="11" text-anchor="end" fill="#666">{I.num(lang, v)}</text>')
    step = max(1, len(x) // 10)
    for i in range(0, len(x), step):
        out.append(f'<text x="{px(i):.1f}" y="{h - pb + 16}" font-size="11" text-anchor="middle" fill="#666">{html.escape(str(x[i]))}</text>')
    for j, s in enumerate(series):
        pts = " ".join(f"{px(i):.1f},{py(float(v)):.1f}" for i, v in enumerate(s["values"]) if v is not None)
        dash = ' stroke-dasharray="6,4"' if s.get("dash") else ""
        c = s.get("color", colors[j % len(colors)])
        out.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2.5"{dash}/>')
        out.append(f'<rect x="{pl + 10 + j * 175}" y="6" width="12" height="4" fill="{c}"/>'
                   f'<text x="{pl + 26 + j * 175}" y="12" font-size="11" fill="#333">{html.escape(s["name"])[:26]}</text>')
    out.append("</svg>")
    return "".join(out)


def _fmt_cell(lang, v):
    if isinstance(v, bool):
        return "✓" if v else "–"
    if isinstance(v, (int, np.integer)):
        return I.num(lang, v)
    if isinstance(v, (float, np.floating)):
        if float(v).is_integer():
            return I.num(lang, v)
        return I.num(lang, v, 0 if abs(v) >= 100 else 1 if abs(v) >= 10 else 2)
    return "–" if v is None else str(v)


CSS = """
:root{--ink:#1d2a24;--muted:#5b6770;--brand:#2f6f4f;--line:#e2e8e4;--bg:#fff}
*{box-sizing:border-box}body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg);margin:0;line-height:1.5}
main{max-width:860px;margin:0 auto;padding:40px 28px}
header{border-bottom:3px solid var(--brand);padding-bottom:14px;margin-bottom:22px}
.brand{color:var(--brand);font-weight:700;letter-spacing:.04em;font-size:13px;text-transform:uppercase}
h1{font-size:28px;margin:6px 0 4px}h2{font-size:19px;margin:28px 0 8px;color:var(--brand)}
.meta{color:var(--muted);font-size:13px}.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;margin-top:8px}
.draft{background:#fff3cd;color:#7a5b00}.ok{background:#d8f0dd;color:#1f5c2e}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 4px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f4f7f5;font-weight:600}th.n,td.n{text-align:right;font-variant-numeric:tabular-nums}
.note{font-size:12px;color:var(--muted);border-top:1px solid var(--line);margin-top:30px;padding-top:12px}
.toolbar{position:sticky;top:0;background:#f4f7f5;padding:8px 28px;display:flex;gap:10px;justify-content:flex-end;border-bottom:1px solid var(--line)}
.toolbar a,.toolbar button{font:inherit;font-size:13px;border:1px solid var(--brand);color:var(--brand);background:#fff;border-radius:6px;padding:4px 12px;cursor:pointer;text-decoration:none}
@media print{.toolbar{display:none}main{padding:0}h2{break-after:avoid}table,svg{break-inside:avoid}}
"""


def render_html(rep: dict) -> str:
    lang = rep["lang"]
    parts = [f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>{html.escape(rep["title"])}</title><style>{CSS}</style></head><body>',
             f'<div class="toolbar"><a href="/api/reports/{rep["id"]}.md">{I.t(lang, "reports.markdown")}</a>'
             f'<button onclick="window.print()">{I.t(lang, "reports.print")}</button></div><main><header>',
             f'<div class="brand">Juuri · {html.escape(I.t(lang, "app.tagline"))}</div><h1>{html.escape(rep["title"])}</h1>',
             f'<div class="meta">{html.escape(I.t(lang, "report.generated", date=rep["created"][:16].replace("T", " ")))}</div>']
    if rep.get("provisional"):
        parts.append(f'<div class="badge draft">{html.escape(I.t(lang, "report.status_draft", n=rep["provisional"]))}</div>')
    else:
        parts.append(f'<div class="badge ok">{html.escape(I.t(lang, "report.status_ok"))}</div>')
    parts.append("</header>")
    for s in rep["sections"]:
        if s.get("heading"):
            parts.append(f"<h2>{html.escape(s['heading'])}</h2>")
        if s.get("text"):
            parts.append(md.markdown(s["text"], extensions=["tables"]))
        if s.get("chart"):
            ch = s["chart"]
            parts.append(_svg_line(ch, lang) if ch.get("kind") == "line" else _svg_hbar(ch, lang))
        if s.get("table"):
            tb = s["table"]
            isnum = [bool(tb["rows"]) and all(isinstance(r[i], (int, float)) and not isinstance(r[i], bool) for r in tb["rows"] if i < len(r) and r[i] is not None)
                     for i in range(len(tb["columns"]))]
            parts.append("<table><thead><tr>" + "".join(f'<th class="{"n" if isnum[i] else ""}">{html.escape(str(c))}</th>'
                                                        for i, c in enumerate(tb["columns"])) + "</tr></thead><tbody>")
            for r in tb["rows"]:
                parts.append("<tr>" + "".join(
                    f'<td class="{"n" if isinstance(v, (int, float)) and not isinstance(v, bool) else ""}">{html.escape(_fmt_cell(lang, v))}</td>'
                    for v in r) + "</tr>")
            parts.append("</tbody></table>")
    if rep.get("sources"):
        parts.append(f"<h2>{html.escape(I.t(lang, 'report.sources'))}</h2><ul>" +
                     "".join(f"<li>{html.escape(s)}</li>" for s in rep["sources"]) + "</ul>")
    parts.append(f'<p class="note">{html.escape(I.t(lang, "report.synthetic"))}</p></main></body></html>')
    return "".join(parts)


def render_md(rep: dict) -> str:
    lang = rep["lang"]
    out = [f"# {rep['title']}", "", I.t(lang, "report.generated", date=rep["created"][:16].replace("T", " ")), ""]
    out.append(I.t(lang, "report.status_draft", n=rep["provisional"]) if rep.get("provisional") else I.t(lang, "report.status_ok"))
    for s in rep["sections"]:
        out.append("")
        if s.get("heading"):
            out += [f"## {s['heading']}", ""]
        if s.get("text"):
            out.append(s["text"])
        if s.get("chart") and not s.get("table"):
            ch = s["chart"]
            out += ["", "| | " + " | ".join(x["name"] for x in ch["series"]) + " |", "|---|" + "---|" * len(ch["series"])]
            for i, lab in enumerate(ch["labels"]):
                out.append(f"| {lab} | " + " | ".join(_fmt_cell(lang, x["values"][i]) for x in ch["series"]) + " |")
        if s.get("table"):
            tb = s["table"]
            out += ["", "| " + " | ".join(map(str, tb["columns"])) + " |", "|" + "---|" * len(tb["columns"])]
            for r in tb["rows"]:
                out.append("| " + " | ".join(_fmt_cell(lang, v).replace("|", "/") for v in r) + " |")
    if rep.get("sources"):
        out += ["", f"## {I.t(lang, 'report.sources')}", ""] + [f"- {s}" for s in rep["sources"]]
    out += ["", f"_{I.t(lang, 'report.synthetic')}_", ""]
    return "\n".join(out)


# ------------------------------------------------------------------------------ standard reports
def _inv_key(T, year):
    from . import inventory as INV
    s = INV.summary(T, year).set_index("category").tco2e
    fl = INV.flag_split(T, year)
    tot = s["scope1_total"] + s["scope2_market_total"] + s["scope3_total"]
    return s, fl, tot


def report_inventory(lang="en", year=2025) -> dict:
    from . import inventory as INV
    T = INV.canonical()
    s, fl, tot = _inv_key(T, year)
    rep = new_report("inventory", lang, f"{I.t(lang, 'rtype.inventory')} {year}", dict(year=year))
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.inv.intro", year=year, s1=I.tonnes(lang, s["scope1_total"]),
                                                 s2m=I.tonnes(lang, s["scope2_market_total"]), s3=I.tonnes(lang, s["scope3_total"]),
                                                 flag=I.pct(lang, 100 * fl.flag_tco2e.sum() / tot)))
    inv = INV.line_items(T, year)
    cat = inv[inv.scope != "scope2_location"].groupby(["scope", "category"]).tco2e.sum().reset_index().sort_values("tco2e", ascending=False)
    section(rep, I.t(lang, "report.inv.by_category"),
            chart=dict(kind="hbar", labels=[I.label(lang, "cat", c) for c in cat.category], series=[dict(name="t CO2e", values=cat.tco2e.round(1).tolist())]),
            table=df_table(pd.DataFrame(dict(a=[I.label(lang, "scope", x) for x in cat.scope], b=[I.label(lang, "cat", c) for c in cat.category],
                                             c=cat.tco2e.round(1), d=(100 * cat.tco2e / tot).round(1))),
                           [col(lang, "scope"), col(lang, "category"), col(lang, "tco2e"), col(lang, "share") + " %"]))
    top = inv[inv.scope.isin(["scope1", "scope3"])].sort_values("tco2e", ascending=False).head(10)
    section(rep, I.t(lang, "report.inv.top_lines"), table=df_table(top[["line_item", "category", "tco2e", "basis"]].assign(
        category=[I.label(lang, "cat", c) for c in top.category], tco2e=top.tco2e.round(1)),
        [col(lang, "line_item"), col(lang, "category"), col(lang, "tco2e"), col(lang, "basis")]))
    fd = T["farm_detail"]
    fd = fd[(fd.year == year) & (fd.region == "FI_SW")]
    if len(fd):
        section(rep, None, I.t(lang, "report.inv.peat", share=I.pct(lang, 100 * fd.organic_tco2e.sum() / fd.tco2e.sum())))
    rep["sources"] = ["GHG Protocol Corporate & Scope 3 Standards; IPCC AR6 GWP100", "package tools/ccn_tools/reference_calculator.py",
                      "outputs/lineage_2025.csv, outputs/audit_log.csv", "docs/METHODOLOGY.md (package)"]
    return save(rep)


def report_quality(lang="en", year=2025) -> dict:
    from . import inventory as INV
    T = INV.canonical()
    iss = T["_issues"]
    x = iss[iss.year == year]
    rep = new_report("quality", lang, f"{I.t(lang, 'rtype.quality')} {year}", dict(year=year))
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.q.intro", n=len(x), year=year, applied=int(x.applied.sum()),
                                                 approved=int((x.status == "approved").sum()), rejected=int((x.status == "rejected").sum())))
    g = x.groupby("issue_type").size().sort_values(ascending=False)
    section(rep, I.t(lang, "report.q.by_type"), chart=dict(kind="hbar", labels=[I.label(lang, "issue", k) for k in g.index],
                                                           series=[dict(name=col(lang, "issues"), values=g.tolist())]))
    pend = x[x.status == "pending"].sort_values("confidence", ascending=False).head(15)
    section(rep, I.t(lang, "report.q.top"), table=df_table(pend[["record_key", "issue_type", "old_value", "new_value", "confidence"]].assign(
        issue_type=[I.label(lang, "issue", k) for k in pend.issue_type]),
        [col(lang, "record"), col(lang, "type"), I.t(lang, "quality.col.old"), col(lang, "proposed"), col(lang, "confidence")]))
    rep["sources"] = ["outputs/issues_all_years.csv", "review/decisions.csv", "juuri/quality.py (rules)"]
    return save(rep)


def report_targets(lang="en", scenario="net_zero_2050", target_year=2032) -> dict:
    from . import pathways as P
    b = P.base_inventory()
    tg = P.targets(b, target_year=target_year)
    pw = P.pathway(b, scenario, None, end=2035, target_year=target_year)
    rep = new_report("targets", lang, f"{I.t(lang, 'rtype.targets')} · {scenario}", dict(scenario=scenario, target_year=target_year))
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.t.intro", base=2025, ty=target_year))
    section(rep, I.t(lang, "report.t.targets"), table=df_table(pd.DataFrame(dict(
        a=[I.t(lang, f"boundary.{k}") for k in tg.boundary], b=tg.base_tco2e.round(0), c=tg.target_tco2e.round(0), d=tg.reduction_pct.round(1))),
        [col(lang, "boundary"), col(lang, "base"), col(lang, "target") + f" {target_year}", col(lang, "reduction") + " %"]))
    section(rep, I.t(lang, "report.t.pathway", scenario=scenario), chart=dict(kind="line", labels=pw.year.tolist(), series=[
        dict(name=I.t(lang, "targets.bau") + " (FLAG)", values=pw.bau_flag.round(0).tolist(), color="#9aa5ad"),
        dict(name=I.t(lang, "targets.with_levers") + " (FLAG)", values=pw.net_flag.round(0).tolist(), color="#2f6f4f"),
        dict(name=I.t(lang, "targets.target") + " (FLAG)", values=pw.target_flag.round(0).tolist(), color="#b23a48", dash=True)]))
    r = pw.set_index("year").loc[target_year]
    section(rep, None, I.t(lang, "report.t.gap", ty=target_year, gap=I.tonnes(lang, r.gap_scope12), gap3=I.tonnes(lang, r.gap_scope3_nonflag),
                           gapf=I.tonnes(lang, r.gap_flag)))
    m = _tr_levers(P.macc(b, scenario).head(10), lang)
    section(rep, I.t(lang, "report.t.levers"), table=df_table(m[["lever_id", "name", "abatement_tco2e", "cost_eur_per_t_scenario", "owner"]].round(0),
                                                              ["ID", col(lang, "lever"), col(lang, "abatement"), col(lang, "cost"), col(lang, "owner")]))
    rep["sources"] = ["SBTi Corporate Net-Zero Standard V1.3.1 / V2.0; SBTi FLAG guidance", "05_scenarios/transition_scenarios.csv (NGFS Phase V, illustrative)",
                      "05_scenarios/decarbonisation_levers.csv; abatement recomputed from the 2025 inventory where possible"]
    return save(rep)


def report_climate(lang="en") -> dict:
    from . import climate as CL
    risk, eur = CL.c2_risk(), CL.c3_euro_risk()
    rep = new_report("climate", lang, I.t(lang, "rtype.climate"))
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.c.intro"))
    r = risk.sort_values(["scenario", "region", "crop"])
    section(rep, I.t(lang, "report.c.risk"), table=df_table(pd.DataFrame(dict(
        a=[I.label(lang, "crop", c) for c in r.crop], b=[region(lang, x) for x in r.region], c=r.scenario, d=r.mean_yield_change_pct.round(1),
        e=r.p_bad_year.round(2))), [col(lang, "crop"), col(lang, "region"), col(lang, "scenario"), col(lang, "change") + " %", col(lang, "p_bad")]))
    e = eur[eur.scenario == "ssp585"].sort_values("expected_cost_eur", ascending=False)
    section(rep, I.t(lang, "report.c.eur") + " (SSP5-8.5)", chart=dict(kind="hbar", labels=[f"{I.label(lang, 'crop', c)} · {region(lang, g)}" for c, g in zip(e.crop, e.region)],
                                                                      series=[dict(name="€", values=(e.expected_cost_eur / 1000).round(0).tolist())], color="#c9a227"),
            table=df_table(pd.DataFrame(dict(a=[I.label(lang, "crop", c) for c in e.crop], b=[region(lang, x) for x in e.region],
                                             c=e.expected_shortfall_t.round(0), d=(e.expected_cost_eur / 1000).round(0), f=(e.p95_cost_eur / 1000).round(0))),
                           [col(lang, "crop"), col(lang, "region"), col(lang, "shortfall"), col(lang, "cost_k"), col(lang, "p95_k")]))
    w = e.iloc[0]
    section(rep, None, I.t(lang, "report.c.worst", crop=I.label(lang, "crop", w.crop), region=region(lang, w.region),
                           eur=I.eur(lang, w.expected_cost_eur), p95=I.eur(lang, w.p95_cost_eur)))
    section(rep, I.t(lang, "report.recommendations"), "- " + I.t(lang, "report.rec.climate"))
    rep["sources"] = ["04_climate/weather_daily_1991_2060.parquet (SSP1-2.6 / 2-4.5 / 5-8.5)", "03_farming survey yields + Foodhills deliveries",
                      "05_scenarios/contract_prices_and_replacement_premiums.csv", "outputs/C_backtest_2020_2026.csv (model validation)"]
    return save(rep)


def report_nature(lang="en") -> dict:
    from . import nature as N
    pr = N.d1_priority()
    rep = new_report("nature", lang, I.t(lang, "rtype.nature"))
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.n.intro"))
    top = pr.head(10)
    section(rep, I.t(lang, "report.n.top"), table=df_table(pd.DataFrame(dict(
        a=top.priority_rank, b=top.catchment_id, c=[WATER.get(w, {}).get(lang, w) for w in top.receiving_water_type],
        d=[STATUS.get(s, {}).get(lang, s) for s in top.ecological_status_class], e=top.apetit_p_to_water_kg.round(0), f=top.apetit_n_to_water_kg.round(0))),
        [col(lang, "rank"), col(lang, "catchment"), "", col(lang, "status"), col(lang, "p"), col(lang, "n")]))
    hs = N.parcel_hotspots(10)
    section(rep, I.t(lang, "report.n.hotspots"), table=df_table(hs[["parcel_id", "catchment_id", "crop", "area_ha", "p_kg", "action"]].assign(
        crop=[I.label(lang, "crop", c) for c in hs.crop], area_ha=hs.area_ha.round(1), p_kg=hs.p_kg.round(1),
        action=[action(lang, a) for a in hs.action]),
        [col(lang, "parcel"), col(lang, "catchment"), col(lang, "crop"), col(lang, "area"), col(lang, "p"), col(lang, "action")]))
    d2 = N.d2_screening().head(8)
    section(rep, I.t(lang, "report.n.d2"), table=df_table(d2[["material", "origin_country", "tco2e", "materiality_score", "why"]].assign(tco2e=d2.tco2e.round(0)),
                                                          [col(lang, "material"), col(lang, "origin"), col(lang, "tco2e"), col(lang, "score"), col(lang, "why")]))
    section(rep, I.t(lang, "report.recommendations"), "- " + I.t(lang, "report.rec.nature"))
    rep["sources"] = ["SBTN steps 1-2; priority = (P/median + 0.5·N/median) × state weight × P sensitivity", "06_nature parcels, catchments, monitored loads",
                      "06_nature/origin_state_of_nature_indicative.csv (indicative)"]
    return save(rep)


def report_farm(lang="en", farm_id="F0001") -> dict:
    from . import inventory as INV, nature as N
    from .data import raw
    T = INV.canonical()
    fr = T["_tables"]["farm_registry"].set_index("farm_id")
    if farm_id not in fr.index:
        raise KeyError(I.t(lang, "chat.farm_unknown", id=farm_id))
    f = fr.loc[farm_id]
    rep = new_report("farm", lang, f"{I.t(lang, 'rtype.farm')} {farm_id}", dict(farm_id=farm_id))
    fd = T["farm_detail"]
    g = fd[(fd.farm_id == farm_id) & (fd.year == 2025) & (fd.region == "FI_SW")]
    crops = ", ".join(I.label(lang, "crop", c) for c in str(f.contract_crops).split(";"))
    if len(g):
        section(rep, I.t(lang, "report.summary"), I.t(lang, "report.f.intro", farm=farm_id, region=region(lang, f.region), crops=crops,
                                                     t=I.tonnes(lang, g.tco2e.sum()), flag=I.tonnes(lang, g.flag_tco2e.sum()),
                                                     org=I.pct(lang, 100 * g.organic_share.max())))
        section(rep, None, table=df_table(g[["crop", "area", "delivered_kg", "kg_ha", "tco2e", "source"]].assign(
            crop=[I.label(lang, "crop", c) for c in g.crop], area=g.area.round(1), delivered_kg=(g.delivered_kg / 1000).round(1),
            kg_ha=(g.kg_ha / 1000).round(2), tco2e=g.tco2e.round(1)),
            [col(lang, "crop"), col(lang, "area"), "t", "t CO2e/ha", col(lang, "tco2e"), col(lang, "basis")]))
    else:
        section(rep, I.t(lang, "report.summary"), f"{farm_id} · {region(lang, f.region)} · {crops}\n\n" + I.t(lang, "report.f.none"))
    loads = N.d1_loads()
    p = loads[loads.farm_id == farm_id]
    if len(p):
        section(rep, I.t(lang, "report.f.parcels"), table=df_table(p[["parcel_id", "crop", "area_ha", "soil_class_soilmap", "catchment_id", "p_kg", "n_kg"]].assign(
            crop=[I.label(lang, "crop", c) for c in p.crop], p_kg=p.p_kg.round(2), n_kg=p.n_kg.round(1)),
            [col(lang, "parcel"), col(lang, "crop"), col(lang, "area"), col(lang, "soil"), col(lang, "catchment"), col(lang, "p"), col(lang, "n")]))
    iss = T["_issues"]
    fi = iss[iss.record_key.isin(set(raw()["farm_survey"].pipe(lambda d: d[d.farm_id == farm_id]).record_key) | {f"farm:{farm_id}"})]
    if len(fi):
        section(rep, I.t(lang, "report.f.issues"), table=df_table(fi[["record_key", "year", "issue_type", "old_value", "new_value", "status"]].assign(
            issue_type=[I.label(lang, "issue", k) for k in fi.issue_type], status=[I.label(lang, "rstatus", s) for s in fi.status]),
            [col(lang, "record"), col(lang, "year"), col(lang, "type"), I.t(lang, "quality.col.old"), col(lang, "proposed"), col(lang, "status")]))
    rep["sources"] = ["01_master/farm_registry.csv, parcels.csv; 03_farming/farm_survey_fi.csv", "METHODOLOGY §5 farm footprint; soil map (~88 % accurate)"]
    return save(rep)


def report_catchment(lang="en", catchment_id="CA01") -> dict:
    from . import nature as N
    pr = N.d1_priority().set_index("catchment_id")
    if catchment_id not in pr.index:
        raise KeyError(catchment_id)
    c = pr.loc[catchment_id]
    rep = new_report("catchment", lang, f"{I.t(lang, 'rtype.catchment')} {catchment_id}", dict(catchment_id=catchment_id))
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.ca.intro", id=catchment_id, water=WATER.get(c.receiving_water_type, {}).get(lang, c.receiving_water_type),
                                                 status=STATUS.get(c.ecological_status_class, {}).get(lang, c.ecological_status_class),
                                                 p=I.num(lang, c.apetit_p_to_water_kg), n=I.num(lang, c.apetit_n_to_water_kg),
                                                 rank=int(c.priority_rank), total=len(pr)))
    loads = N.d1_loads()
    p = loads[loads.catchment_id == catchment_id].sort_values("p_kg", ascending=False).head(15)
    hs = N.parcel_hotspots(5000).set_index("parcel_id").action
    section(rep, I.t(lang, "report.n.hotspots"), table=df_table(p[["parcel_id", "farm_id", "crop", "area_ha", "p_kg", "n_kg"]].assign(
        crop=[I.label(lang, "crop", x) for x in p.crop], p_kg=p.p_kg.round(2), n_kg=p.n_kg.round(1), action=[action(lang, a) for a in p.parcel_id.map(hs).fillna("")]),
        [col(lang, "parcel"), "ID", col(lang, "crop"), col(lang, "area"), col(lang, "p"), col(lang, "n"), col(lang, "action")]))
    rep["sources"] = ["06_nature/catchments.csv, monitored_catchment_loads.csv", "juuri/nature.py field-to-water model (calibrated on dev catchments)"]
    return save(rep)


def report_executive(lang="en") -> dict:
    from . import climate as CL, inventory as INV, nature as N, pathways as P
    T = INV.canonical()
    s, fl, tot = _inv_key(T, 2025)
    rep = new_report("executive", lang, I.t(lang, "rtype.executive") + " 2025")
    fd = T["farm_detail"]
    fd = fd[(fd.year == 2025) & (fd.region == "FI_SW")]
    section(rep, I.t(lang, "report.summary"), I.t(lang, "report.e.intro"))
    section(rep, I.t(lang, "report.key_figures"), table=df_table(pd.DataFrame(dict(a=[
        I.t(lang, "kpi.scope1"), I.t(lang, "kpi.scope2m"), I.t(lang, "kpi.scope3"), I.t(lang, "kpi.flag_share"), I.t(lang, "kpi.peat_share")], b=[
        I.tonnes(lang, s["scope1_total"]), I.tonnes(lang, s["scope2_market_total"]), I.tonnes(lang, s["scope3_total"]),
        I.pct(lang, 100 * fl.flag_tco2e.sum() / tot), I.pct(lang, 100 * fd.organic_tco2e.sum() / fd.tco2e.sum())])), ["", ""]))
    b = P.base_inventory()
    tg = P.targets(b)
    section(rep, I.t(lang, "report.t.targets"), table=df_table(pd.DataFrame(dict(
        a=[I.t(lang, f"boundary.{k}") for k in tg.boundary], b=tg.base_tco2e.round(0), c=tg.target_tco2e.round(0))),
        [col(lang, "boundary"), col(lang, "base") + " 2025", col(lang, "target") + " 2032"]))
    m = _tr_levers(P.macc(b).head(5), lang)
    section(rep, I.t(lang, "report.t.levers"), table=df_table(m[["name", "abatement_tco2e", "cost_eur_per_t_scenario", "owner"]].round(0),
                                                              [col(lang, "lever"), col(lang, "abatement"), col(lang, "cost"), col(lang, "owner")]))
    e = CL.c3_euro_risk()
    e = e[e.scenario == "ssp585"].sort_values("expected_cost_eur", ascending=False).iloc[0]
    section(rep, I.t(lang, "rtype.climate"), I.t(lang, "report.c.worst", crop=I.label(lang, "crop", e.crop), region=region(lang, e.region),
                                                 eur=I.eur(lang, e.expected_cost_eur), p95=I.eur(lang, e.p95_cost_eur)))
    pr = N.d1_priority().head(3)
    section(rep, I.t(lang, "rtype.nature"), ", ".join(f"**{r.catchment_id}** ({STATUS.get(r.ecological_status_class, {}).get(lang, r.ecological_status_class)}, "
                                                       f"{I.num(lang, r.apetit_p_to_water_kg)} kg P)" for r in pr.itertuples()))
    recs = ["report.rec.review", "report.rec.flag", "report.rec.peat", "report.rec.climate", "report.rec.nature"]
    section(rep, I.t(lang, "report.recommendations"), "\n".join(f"{i}. {I.t(lang, k)}" for i, k in enumerate(recs, 1)))
    rep["sources"] = ["GHG Protocol; SBTi CNZS V1.3.1/V2.0 and FLAG; SBTN steps 1-2; NGFS Phase V (illustrative)", "Juuri outputs/ (reproducible with python -m juuri run)"]
    return save(rep)


STANDARD = {"executive": report_executive, "inventory": report_inventory, "quality": report_quality, "targets": report_targets,
            "climate": report_climate, "nature": report_nature, "farm": report_farm, "catchment": report_catchment}


def standard(kind: str, lang: str = "en", **params) -> dict:
    fn = STANDARD[kind]
    import inspect
    allowed = set(inspect.signature(fn).parameters) - {"lang"}
    return fn(lang=lang, **{k: v for k, v in params.items() if k in allowed and v not in (None, "")})


def custom(lang: str, title: str, sections: list[dict], sources: list[str] | None = None) -> dict:
    """Report composed by the assistant. Sections: heading, text (Markdown), optional table {columns, rows},
    optional chart {kind: bar|line, labels, series:[{name, values}]}."""
    rep = new_report("custom", lang, title)
    for s in sections:
        ch = s.get("chart")
        if ch and ch.get("kind") == "bar":
            ch["kind"] = "hbar"
        section(rep, s.get("heading"), s.get("text"), s.get("table"), ch)
    rep["sources"] = sources or []
    return save(rep)
