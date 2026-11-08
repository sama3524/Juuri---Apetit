/* Juuri web app — vanilla JS single-page app over the Juuri API. */
"use strict";

const URL_LANG = new URLSearchParams(location.search).get("lang");
const S = {
  lang: URL_LANG || localStorage.getItem("juuri.lang") || (navigator.language || "en").slice(0, 2),
  policy: localStorage.getItem("juuri.policy") || "provisional",
  dict: {}, en: {}, meta: null, cat: { lever: {}, owner: {}, confidence: {} },
  chat: { messages: [], conversationId: null, busy: false },
};
if (!["en", "fi", "sv"].includes(S.lang)) S.lang = "en";
const LOCALE = { en: "en-GB", fi: "fi-FI", sv: "sv-SE" };
const COLORS = { s1: "#2f6f4f", s2: "#7aa95c", s3: "#c9a227", flag: "#8c5a2b", nonflag: "#5b7f95", target: "#b23a48", bau: "#9aa5ad", net: "#2f6f4f" };

const PAGES = [
  ["overview", "◎", renderOverview], ["assistant", "💬", renderAssistant], ["reports", "📄", renderReports],
  ["quality", "🔎", renderQuality], ["inventory", "📊", renderInventory], ["targets", "🎯", renderTargets],
  ["climate", "🌦", renderClimate], ["nature", "💧", renderNature], ["live", "📡", renderLive], ["method", "📚", renderMethod],
];

/* ---------------------------------------------------------------- utilities */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function t(key, vars = {}) {
  let s = S.dict[key] ?? S.en[key] ?? key;
  for (const [k, v] of Object.entries(vars)) s = s.replaceAll("{" + k + "}", v);
  return s;
}
const lbl = (prefix, code) => S.dict[`${prefix}.${code}`] ?? S.en[`${prefix}.${code}`] ?? code;
function num(x, d = 0) { if (x === null || x === undefined || Number.isNaN(x)) return "–"; return new Intl.NumberFormat(LOCALE[S.lang], { maximumFractionDigits: d, minimumFractionDigits: d }).format(x); }
const tons = (x) => num(x, Math.abs(x) >= 10 ? 0 : 1) + " t";
const pct = (x, d = 0) => num(x, d) + " %";
const eur = (x) => new Intl.NumberFormat(LOCALE[S.lang], { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(x);
function toast(msg) { const el = $("#toast"); el.textContent = msg; el.classList.add("show"); clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("show"), Math.max(2600, msg.length * 60)); }
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) { /* ignore */ } throw new Error(m); }
  return r.json();
}
const q = (o) => "?" + new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== ""))).toString();
function md(text) { return DOMPurify.sanitize(marked.parse(text || "")); }
function loading(el) { el.innerHTML = `<div class="loading">${esc(t("common.loading"))}</div>`; }
function errorBox(el, e) { el.innerHTML = `<div class="card">⚠️ ${esc(t("common.error"))}: ${esc(e.message || e)}</div>`; }
function plot(el, data, layout = {}) {
  const fullLayout = Object.assign({ margin: { l: 64, r: 16, t: 10, b: 40 }, font: { family: "Inter, sans-serif", size: 12 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", legend: { orientation: "h", y: -0.18 }, separators: S.lang === "en" ? ".," : ", " }, layout);
  if (fullLayout.height) el.style.height = fullLayout.height + "px";   // container must match the plot or content below overlaps
  const p = Plotly.newPlot(el, data, fullLayout, { displayModeBar: false, responsive: true, locale: S.lang, doubleClick: "reset+autosize" });
  const isPie = data.every((d) => d.type === "pie");
  if (!isPie && !layout.noControls) p.then(() => chartControls(el), () => {});
  return p;
}

/** Small ＋ / － / ⟲ buttons on a chart (cartesian axes or map). */
function chartControls(el) {
  el.parentElement.querySelectorAll(`.chart-ctl[data-for="${el.id}"]`).forEach((x) => x.remove());
  const box = document.createElement("div");
  box.className = "chart-ctl"; box.dataset.for = el.id;
  box.innerHTML = `<button data-z="in" title="＋">＋</button><button data-z="out" title="－">－</button><button data-z="reset" title="⟲">⟲</button>`;
  el.style.position = "relative";
  el.appendChild(box);
  const fl = el._fullLayout;
  const isMap = !!(fl.map || fl.mapbox);
  const mapKey = fl.map ? "map" : "mapbox";
  const start = isMap ? { zoom: fl[mapKey].zoom, center: { ...fl[mapKey].center } } : null;
  const axes = () => Object.keys(fl).filter((k) => /^[xy]axis\d*$/.test(k) && fl[k].range && fl[k].type !== "category");
  const zoom = (f) => {
    if (isMap) return Plotly.relayout(el, { [`${mapKey}.zoom`]: el._fullLayout[mapKey].zoom + (f < 1 ? 1 : -1) });
    const upd = {};
    for (const k of axes()) {
      const ax = el._fullLayout[k], log = ax.type === "log";
      let [a, b] = ax.range.map(Number);
      const c = (a + b) / 2, h = ((b - a) / 2) * f;
      upd[`${k}.range`] = [c - h, c + h];
      if (log) upd[`${k}.range`] = [c - h, c + h];
    }
    Plotly.relayout(el, upd);
  };
  box.querySelector('[data-z="in"]').onclick = () => zoom(0.6);
  box.querySelector('[data-z="out"]').onclick = () => zoom(1 / 0.6);
  box.querySelector('[data-z="reset"]').onclick = () => {
    if (isMap) return Plotly.relayout(el, { [`${mapKey}.zoom`]: start.zoom, [`${mapKey}.center`]: start.center });
    const upd = {};
    for (const k of Object.keys(el._fullLayout).filter((k) => /^[xy]axis\d*$/.test(k))) upd[`${k}.autorange`] = true;
    Plotly.relayout(el, upd);
  };
}
function kpi(label, value, sub = "") { return `<div class="card kpi"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`; }

/** Render a data table. cols: [{k, label, num?, d?, f?(value,row) -> html}] */
function table(cols, rows, opts = {}) {
  const head = (opts.select ? "<th></th>" : "") + cols.map((c) => `<th class="${c.num ? "n" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => {
    const cells = cols.map((c) => {
      const v = r[c.k];
      const html = c.f ? c.f(v, r) : c.num ? esc(num(v, c.d ?? 0)) : esc(v ?? "–");
      return `<td class="${c.num ? "n" : ""}">${html}</td>`;
    }).join("");
    return `<tr data-i="${i}" class="${opts.onClick ? "clickable" : ""}">${opts.select ? `<td><input type="checkbox" data-sel="${i}"></td>` : ""}${cells}</tr>`;
  }).join("");
  return `<div class="table-wrap" style="${opts.max ? `max-height:${opts.max}px` : ""}"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${cols.length + 1}" class="muted">${esc(t("common.none"))}</td></tr>`}</tbody></table></div>`;
}
function bindRows(container, rows, onClick) {
  container.querySelectorAll("tr.clickable").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.matches("input")) return;
    container.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
    tr.classList.add("sel");
    onClick(rows[+tr.dataset.i]);
  }));
}
const lev = (id, name) => (S.cat.lever[id] || {})[S.lang] || name;
const own = (o) => (S.cat.owner[o] || {})[S.lang] || o;
const conf = (c) => (S.cat.confidence[c] || {})[S.lang] || c;
const act = (a) => ((S.cat.action || {})[a] || {})[S.lang] || a;
const help = (key) => `<p class="help">ℹ️ ${esc(t(key))}</p>`;
const statusPill = (s) => `<span class="pill ${esc(s)}">${esc(lbl("rstatus", s))}</span>`;

/* ---------------------------------------------------------------- shell */
async function loadLang(lang) {
  S.lang = lang; localStorage.setItem("juuri.lang", lang);
  document.documentElement.lang = lang;
  S.dict = await (await fetch(`/static/i18n/${lang}.json`)).json();
  if (!Object.keys(S.cat.lever).length) S.cat = await (await fetch("/static/i18n/catalogue.json")).json();
  if (!Object.keys(S.en).length) S.en = lang === "en" ? S.dict : await (await fetch("/static/i18n/en.json")).json();
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $("#policySel").title = t("policy.help");
  document.querySelectorAll("#langSwitch button").forEach((b) => b.classList.toggle("on", b.dataset.lang === lang));
  renderNav();
}
function renderNav() {
  const cur = (location.hash.slice(2) || "overview").split("?")[0];
  $("#nav").innerHTML = PAGES.map(([id, ico]) => `<a href="#/${id}" class="${id === cur ? "active" : ""}"><span class="ico">${ico}</span>${esc(t("nav." + id))}</a>`).join("");
}
async function refreshMeta() {
  try {
    S.meta = await api("/api/meta");
    const r = S.meta.review;
    $("#statusBadge").innerHTML = r.provisional && S.policy === "provisional"
      ? `<span class="badge draft">${esc(t("status.draft", { n: num(r.provisional) }))}</span>`
      : `<span class="badge ok">${esc(t("status.approved"))}</span>`;
  } catch (e) { /* server warming up */ }
}
async function route() {
  renderNav();
  const id = (location.hash.slice(2) || "overview").split("?")[0];
  const page = PAGES.find((p) => p[0] === id) || PAGES[0];
  const view = $("#view");
  $("#sidebar").classList.remove("open");
  $("#chatFab").style.display = id === "assistant" ? "none" : "";
  if (id === "assistant") $("#drawer").classList.remove("open");
  loading(view);
  try { await page[2](view); } catch (e) { errorBox(view, e); console.error(e); }
}

/* ---------------------------------------------------------------- overview */
async function renderOverview(v) {
  const d = await api("/api/overview" + q({ policy: S.policy }));
  const k = d.kpi;
  v.innerHTML = `<h1>${esc(t("overview.title"))}</h1><p class="lead">${esc(t("overview.lead"))}</p>
    <div class="grid kpis">${kpi(t("kpi.scope1") + " · 2025", tons(k.scope1))}${kpi(t("kpi.scope2m") + " · 2025", tons(k.scope2m))}
    ${kpi(t("kpi.scope3") + " · 2025", tons(k.scope3))}${kpi(t("kpi.flag_share"), pct(k.flag_share))}${kpi(t("kpi.peat_share"), pct(k.peat_share))}</div>
    <div class="grid cols-2"><div class="card"><h2>${esc(t("overview.trend"))}</h2><div id="trend" class="chart"></div></div>
    <div class="card"><h2>${esc(t("overview.actions"))}</h2>${d.actions.map((a) => `<div class="action"><strong>${esc(lev(a.lever_id, a.name))}</strong> — ${esc(tons(a.abatement_tco2e))}/a · ${esc(eur(a.cost_eur_per_t_scenario))}/t
      <div class="ev">${esc(t("overview.owner"))}: ${esc(own(a.owner))} · ${esc(t("overview.confidence"))} ${esc(conf(a.confidence))}</div></div>`).join("")}
      <div class="section"><a class="btn" href="#/assistant">💬 ${esc(t("overview.ask"))}</a></div></div></div>
    <div class="grid kpis section">${kpi(t("kpi.issues"), num(d.review.issues))}${kpi(t("kpi.applied"), num(d.review.applied))}${kpi(t("kpi.approved"), num(d.review.approved))}${kpi(t("kpi.rejected"), num(d.review.rejected))}</div>`;
  const yr = d.trend.map((r) => r.year);
  plot($("#trend"), [
    { type: "bar", name: t("kpi.scope3"), x: yr, y: d.trend.map((r) => r.scope3), marker: { color: COLORS.s3 } },
    { type: "bar", name: t("kpi.scope2m"), x: yr, y: d.trend.map((r) => r.scope2), marker: { color: COLORS.s2 } },
    { type: "bar", name: t("kpi.scope1"), x: yr, y: d.trend.map((r) => r.scope1), marker: { color: COLORS.s1 } },
    { type: "scatter", mode: "lines+markers", name: t("overview.published"), x: d.published.map((r) => r.year), y: d.published.map((r) => r.total), line: { color: "#333", dash: "dot" } },
  ], { barmode: "stack", yaxis: { title: "t CO2e" }, height: 340 });
}

/* ---------------------------------------------------------------- assistant */
function chatWidget(el, compact) {
  el.innerHTML = `${compact ? "" : `<div class="examples">${[1, 2, 3, 4].map((i) => `<button data-ex="${i}">${esc(t("assistant.ex" + i))}</button>`).join("")}</div>`}
    <div class="msgs"></div>
    <div class="composer"><textarea rows="2" placeholder="${esc(t("assistant.placeholder"))}"></textarea>
      <div style="display:flex;flex-direction:column;gap:6px"><button class="btn send">${esc(t("assistant.send"))}</button>
      <button class="btn ghost new" title="${esc(t("assistant.new_chat"))}">↺</button></div></div>`;
  const box = $(".msgs", el), ta = $("textarea", el);
  const paint = () => {
    box.innerHTML = S.chat.messages.map((m) => m.role === "user" ? `<div class="msg user">${esc(m.text)}</div>`
      : `<div class="msg bot">${md(m.text)}${(m.reports || []).map((r) => `<div class="report-card">📄<span class="t">${esc(r.title)}</span>
          <a class="btn ghost" target="_blank" href="/api/reports/${esc(r.id)}.html">${esc(t("common.open"))}</a>
          <a class="btn ghost" href="/api/reports/${esc(r.id)}.md">MD</a></div>`).join("")}
          ${m.trace && m.trace.length ? `<details><summary>${esc(t("assistant.evidence"))}</summary><pre>${esc(JSON.stringify(m.trace, null, 1))}</pre></details>` : ""}</div>`).join("")
      + (S.chat.busy ? `<div class="msg bot typing">${esc(t("assistant.thinking"))}</div>` : "");
    box.scrollTop = box.scrollHeight;
  };
  const send = async (text) => {
    text = (text || "").trim();
    if (!text || S.chat.busy) return;
    S.chat.messages.push({ role: "user", text }); S.chat.busy = true; ta.value = ""; repaintAll();
    try {
      const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text, lang: S.lang, conversation_id: S.chat.conversationId }) });
      S.chat.conversationId = r.conversation_id;
      S.chat.messages.push({ role: "bot", text: r.text, reports: r.reports, trace: r.trace });
    } catch (e) { S.chat.messages.push({ role: "bot", text: "⚠️ " + e.message }); }
    S.chat.busy = false; repaintAll();
  };
  $(".send", el).onclick = () => send(ta.value);
  $(".new", el).onclick = () => { S.chat = { messages: [], conversationId: null, busy: false }; repaintAll(); };
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(ta.value); } });
  el.querySelectorAll("[data-ex]").forEach((b) => b.onclick = () => send(b.textContent));
  el._paint = paint;
  paint();
}
function repaintAll() { document.querySelectorAll(".chat").forEach((c) => c._paint && c._paint()); }
async function renderAssistant(v) {
  v.innerHTML = `<h1>${esc(t("assistant.title"))}</h1><p class="lead">${esc(t("assistant.lead"))}</p><div class="card"><div id="mainChat" class="chat"></div></div>`;
  chatWidget($("#mainChat"), false);
  const ask = new URLSearchParams(location.hash.split("?")[1] || "").get("q");   // deep link: #/assistant?q=...
  if (ask && !S.chat.messages.some((m) => m.text === ask)) { $("#mainChat textarea").value = ask; $("#mainChat .send").click(); }
}

/* ---------------------------------------------------------------- reports */
async function renderReports(v) {
  const d = await api("/api/reports");
  v.innerHTML = `<h1>${esc(t("reports.title"))}</h1><p class="lead">${esc(t("reports.lead"))}</p>
    <div class="card"><div class="filters">
      <label>${esc(t("reports.type"))}<select id="rk">${d.kinds.map((k) => `<option value="${k}">${esc(t("rtype." + k))}</option>`).join("")}</select></label>
      <label>${esc(t("reports.language"))}<select id="rl">${["fi", "sv", "en"].map((l) => `<option value="${l}" ${l === S.lang ? "selected" : ""}>${l.toUpperCase()}</option>`).join("")}</select></label>
      <label id="ryw">${esc(t("common.year"))}<select id="ry">${[2025, 2024, 2023, 2022, 2021].map((y) => `<option>${y}</option>`).join("")}</select></label>
      <label id="rsw">${esc(t("reports.subject"))}<input type="text" id="rs" placeholder="F0123 / CA12" size="10"></label>
      <button class="btn" id="rgo">${esc(t("reports.generate"))}</button></div></div>
    <div class="card section"><h2>${esc(t("reports.list"))}</h2><div id="rlist"></div></div>`;
  const sync = () => { const k = $("#rk").value; $("#ryw").style.display = ["inventory", "quality"].includes(k) ? "" : "none"; $("#rsw").style.display = ["farm", "catchment"].includes(k) ? "" : "none"; };
  $("#rk").onchange = sync; sync();
  const list = (rows) => { $("#rlist").innerHTML = table([
    { k: "title", label: t("reports.title"), f: (x, r) => `<a target="_blank" href="/api/reports/${esc(r.id)}.html">${esc(x)}</a>` },
    { k: "kind", label: t("reports.type"), f: (x) => esc(t("rtype." + x)) }, { k: "lang", label: t("reports.language"), f: (x) => esc(x.toUpperCase()) },
    { k: "created", label: t("reports.created"), f: (x) => esc(x.replace("T", " ").slice(0, 16)) },
    { k: "id", label: "", f: (x) => `<a href="/api/reports/${esc(x)}.html" target="_blank">${esc(t("reports.print"))}</a> · <a href="/api/reports/${esc(x)}.md">${esc(t("reports.markdown"))}</a>` }], rows); };
  list(d.reports);
  $("#rgo").onclick = async () => {
    const kind = $("#rk").value, subj = $("#rs").value.trim().toUpperCase();
    const params = { year: +$("#ry").value };
    if (kind === "farm") params.farm_id = subj || "F0123";
    if (kind === "catchment") params.catchment_id = subj || "CA12";
    $("#rgo").disabled = true;
    try {
      const r = await api("/api/reports", { method: "POST", body: JSON.stringify({ kind, lang: $("#rl").value, params }) });
      window.open(`/api/reports/${r.id}.html`, "_blank");
      list((await api("/api/reports")).reports);
    } catch (e) { toast("⚠️ " + e.message); }
    $("#rgo").disabled = false;
  };
}

/* ---------------------------------------------------------------- data quality & review */
async function renderQuality(v) {
  const st = Object.assign({ year: 2025, table: "", issue_type: "", status: "" }, S.qf || {});
  const d = await api("/api/issues" + q({ policy: S.policy, ...st }));
  const opt = (vals, cur, f = (x) => x) => `<option value="">${esc(t("common.all"))}</option>` + vals.map((x) => `<option value="${esc(x)}" ${String(x) === String(cur) ? "selected" : ""}>${esc(f(x))}</option>`).join("");
  v.innerHTML = `<h1>${esc(t("quality.title"))}</h1><p class="lead">${esc(t("quality.lead"))}</p>
    <div class="card"><div class="filters">
      <label>${esc(t("common.year"))}<select id="fy">${opt(d.facets.years.slice().reverse(), st.year)}</select></label>
      <label>${esc(t("quality.feed"))}<select id="ft">${opt(d.facets.tables, st.table)}</select></label>
      <label>${esc(t("quality.type"))}<select id="fi">${opt(d.facets.types, st.issue_type, (x) => lbl("issue", x))}</select></label>
      <label>${esc(t("quality.status"))}<select id="fs">${opt(["pending", "approved", "rejected"], st.status, (x) => lbl("rstatus", x))}</select></label>
      <span class="spacer"></span><a class="btn ghost" href="/api/issues.csv?policy=${S.policy}">${esc(t("common.download_csv"))}</a>
      <a class="btn ghost" href="/api/audit.csv?policy=${S.policy}">${esc(t("quality.audit"))} CSV</a></div>
    <div class="grid cols-2"><div><div class="muted" id="qcount"></div><div id="qtab"></div></div><div id="qchart" class="chart"></div></div>
    <div class="filters section"><label>${esc(t("quality.reviewer"))}<input type="text" id="rev" value="${esc(localStorage.getItem("juuri.reviewer") || "")}"></label>
      <label style="flex:1">${esc(t("quality.comment"))}<input type="text" id="com"></label>
      <button class="btn" id="ap">${esc(t("quality.approve"))}</button><button class="btn danger" id="rj">${esc(t("quality.reject"))}</button></div></div>`;
  const cols = [{ k: "record_key", label: t("quality.col.record") }, { k: "issue_type", label: t("quality.col.type"), f: (x) => esc(lbl("issue", x)) },
    { k: "field", label: t("quality.col.field") }, { k: "old_value", label: t("quality.col.old") }, { k: "new_value", label: t("quality.col.new") },
    { k: "confidence", label: t("quality.col.conf"), num: true, d: 2 }, { k: "status", label: t("quality.col.status"), f: statusPill },
    { k: "explanation", label: t("quality.col.why"), f: (x) => `<span class="muted">${esc(x)}</span>` }];
  $("#qtab").innerHTML = table(cols, d.rows, { select: true, max: 520 });
  $("#qcount").textContent = t("quality.shown", { n: num(d.total) });
  const bt = Object.entries(d.by_type);
  plot($("#qchart"), [{ type: "bar", orientation: "h", x: bt.map((x) => x[1]), y: bt.map((x) => lbl("issue", x[0])), marker: { color: COLORS.s1 } }],
    { height: Math.max(320, bt.length * 26), margin: { l: 170, r: 10, t: 10, b: 30 } });
  const refilter = () => { S.qf = { year: $("#fy").value, table: $("#ft").value, issue_type: $("#fi").value, status: $("#fs").value }; route(); };
  ["#fy", "#ft", "#fi", "#fs"].forEach((s) => $(s).onchange = refilter);
  const decide = async (decision) => {
    const reviewer = $("#rev").value.trim();
    if (!reviewer) return toast(t("quality.need_reviewer"));
    localStorage.setItem("juuri.reviewer", reviewer);
    const items = [...v.querySelectorAll("[data-sel]:checked")].map((c) => d.rows[+c.dataset.sel]).map((r) => ({ record_key: r.record_key, field: r.field }));
    if (!items.length) return;
    await api("/api/decisions", { method: "POST", body: JSON.stringify({ items, decision, reviewer, comment: $("#com").value }) });
    toast(t("quality.saved", { n: items.length })); await refreshMeta(); route();
  };
  $("#ap").onclick = () => decide("approved"); $("#rj").onclick = () => decide("rejected");
}

/* ---------------------------------------------------------------- inventory */
async function renderInventory(v) {
  const year = S.invYear || 2025;
  const d = await api("/api/inventory" + q({ year, policy: S.policy }));
  const tt = d.totals;
  v.innerHTML = `<h1>${esc(t("inventory.title"))}</h1>
    <div class="filters"><label>${esc(t("common.year"))}<select id="iy">${[2025, 2024, 2023, 2022, 2021].map((y) => `<option ${y === year ? "selected" : ""}>${y}</option>`).join("")}</select></label></div>
    <div class="grid kpis">${kpi(t("kpi.scope1"), tons(tt.scope1))}${kpi(t("kpi.scope2l"), tons(tt.scope2l))}${kpi(t("kpi.scope2m"), tons(tt.scope2m))}${kpi(t("kpi.scope3"), tons(tt.scope3))}${kpi(t("kpi.biogenic"), tons(tt.biogenic))}</div>
    <div class="grid cols-2"><div class="card"><h2>${esc(t("inventory.by_category"))}</h2><div id="cat" class="chart"></div></div>
    <div class="card"><h2>${esc(t("inventory.flag"))}</h2><div id="flag" class="chart"></div></div></div>
    <div class="grid cols-2e section"><div class="card"><h2>${esc(t("inventory.lines"))}</h2><p class="muted">${esc(t("inventory.click"))}</p><div id="lines"></div></div>
    <div class="card"><h2>${esc(t("inventory.lineage"))}</h2><div id="lin" class="muted">—</div></div></div>
    <div class="card section"><h2>${esc(t("inventory.peat"))}</h2><div id="peat"></div></div>`;
  $("#iy").onchange = (e) => { S.invYear = +e.target.value; route(); };
  const cats = d.categories.slice().sort((a, b) => a.tco2e - b.tco2e);
  const col = { scope1: COLORS.s1, scope2_market: COLORS.s2, scope3: COLORS.s3 };
  plot($("#cat"), [{ type: "bar", orientation: "h", x: cats.map((c) => c.tco2e), y: cats.map((c) => lbl("cat", c.category)), marker: { color: cats.map((c) => col[c.scope]) } }],
    { xaxis: { type: "log", title: "t CO2e" }, height: 420, margin: { l: 200, r: 10, t: 10, b: 40 } });
  plot($("#flag"), [{ type: "pie", hole: .55, labels: [t("inventory.flag_land"), t("inventory.nonflag")], values: [d.flag, d.nonflag], marker: { colors: [COLORS.flag, COLORS.nonflag] }, sort: false }], { height: 320 });
  const lines = d.lines.filter((l) => l.tco2e > 0);
  $("#lines").innerHTML = help("help.lines") + table([{ k: "line_item", label: t("col.line_item") }, { k: "category", label: t("col.category"), f: (x) => esc(lbl("cat", x)) },
    { k: "scope", label: t("col.scope"), f: (x) => esc(lbl("scope", x)) }, { k: "tco2e", label: t("col.emissions"), num: true, d: 1 }], lines, { onClick: true, max: 420 });
  bindRows($("#lines"), lines, async (r) => {
    const box = $("#lin"); loading(box);
    const x = await api("/api/lineage" + q({ line_item: r.line_item, scope: r.scope, year, policy: S.policy }));
    box.classList.remove("muted");
    box.innerHTML = `<p><strong>${esc(r.line_item)}</strong> · ${esc(tons(r.tco2e))} · <em>${esc(r.basis)}</em></p>` + help("help.lineage") + (x.n ? table([
      { k: "record_key", label: t("col.record") }, { k: "activity", label: t("col.activity"), num: true, d: 1 }, { k: "unit", label: t("col.unit") }, { k: "ef", label: t("col.ef"), num: true, d: 4 },
      { k: "ef_source", label: t("col.ef_source") }, { k: "tco2e", label: t("col.emissions"), num: true, d: 2 }], x.records, { max: 300 }) + `<div class="ev">${esc(t("inventory.records_sum", { n: num(x.n), t: tons(x.total) }))}</div>` : `<p class="muted">${esc(r.basis)}</p>`)
      + (x.corrections.length ? `<h2 class="section">${esc(t("inventory.corrections"))}</h2>` + table([{ k: "record_key", label: t("quality.col.record") }, { k: "field", label: t("quality.col.field") },
        { k: "old_value", label: t("quality.col.old") }, { k: "new_value", label: t("quality.col.new") }, { k: "issue_type", label: t("quality.col.type"), f: (y) => esc(lbl("issue", y)) },
        { k: "status", label: t("quality.col.status"), f: (s) => statusPill(s) }], x.corrections, { max: 240 }) : "");
  });
  $("#peat").innerHTML = help("help.peat") + table([{ k: "crop", label: t("col.crop"), f: (x) => esc(lbl("crop", x)) }, { k: "juuri_t", label: t("col.juuri_t"), num: true },
    { k: "current_t", label: t("col.current_t"), num: true }, { k: "organic_t", label: t("col.peat_t"), num: true }, { k: "delivered_t", label: t("col.delivered_t"), num: true }], d.peat);
}

/* ---------------------------------------------------------------- targets */
async function renderTargets(v) {
  const st = Object.assign({ scenario: "net_zero_2050", compare: "current_policies", target_year: 2032, boundary: "scope12", levers: "" }, S.tf || {});
  const d = await api("/api/targets" + q({ ...st, policy: S.policy }));
  const B = { scope12: t("boundary.scope12"), scope3_nonflag: t("boundary.scope3_nonflag"), flag: t("boundary.flag") };
  v.innerHTML = `<h1>${esc(t("targets.title"))}</h1>
    <div class="card"><h2>${esc(t("targets.base_year"))}</h2>${help("help.base_years")}${table([{ k: "option", label: t("col.option") }, { k: "scope1", label: t("col.s1"), num: true }, { k: "scope2", label: t("col.s2"), num: true },
      { k: "scope3", label: t("col.s3"), num: true }, { k: "note", label: t("col.note"), f: (x) => `<span class="muted">${esc(x)}</span>` }], d.base_years)}</div>
    <div class="card section"><div class="filters">
      <label>${esc(t("targets.target_year"))}<select id="ty">${[2030, 2031, 2032, 2033, 2034, 2035].map((y) => `<option ${y === +st.target_year ? "selected" : ""}>${y}</option>`).join("")}</select></label>
      <label>${esc(t("common.scenario"))}<select id="sc">${d.scenarios.map((s) => `<option ${s === st.scenario ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label>vs<select id="sc2">${d.scenarios.map((s) => `<option ${s === st.compare ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label>${esc(t("targets.boundary"))}<select id="bd">${Object.entries(B).map(([k, x]) => `<option value="${k}" ${k === st.boundary ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label></div>
      <h2>${esc(t("targets.targets"))}</h2><div class="grid kpis">${d.targets.map((x) => kpi(`${B[x.boundary]} · ${x.target_year}`, tons(x.target_tco2e), `−${pct(x.reduction_pct, 1)} · ${x.source}`)).join("")}</div>
      <h2>${esc(t("targets.pathway"))}</h2><div id="pw" class="chart"></div><p id="gap"></p></div>
    <div class="card section"><h2>${esc(t("targets.macc"))} · ${esc(B[st.boundary])} · ${esc(st.target_year)}</h2><div id="macc" class="chart"></div></div>
    <div class="card section"><h2>${esc(t("targets.levers"))}</h2><div id="lv"></div></div>`;
  const re = () => { S.tf = { scenario: $("#sc").value, compare: $("#sc2").value, target_year: $("#ty").value, boundary: $("#bd").value, levers: d.chosen.join(",") }; route(); };
  ["#ty", "#sc", "#sc2", "#bd"].forEach((s) => $(s).onchange = re);
  const b = st.boundary, traces = [];
  Object.entries(d.pathways).forEach(([sc, rows]) => {
    const dash = sc === st.scenario ? "solid" : "dot";
    traces.push({ x: rows.map((r) => r.year), y: rows.map((r) => r["bau_" + b]), name: `${t("targets.bau")} · ${sc}`, line: { color: COLORS.bau, dash } });
    traces.push({ x: rows.map((r) => r.year), y: rows.map((r) => r["net_" + b]), name: `${t("targets.with_levers")} · ${sc}`, line: { color: COLORS.net, dash, width: 3 } });
  });
  const main = d.pathways[st.scenario];
  traces.push({ x: main.map((r) => r.year), y: main.map((r) => r["target_" + b]), name: t("targets.target"), line: { color: COLORS.target, dash: "dash" } });
  plot($("#pw"), traces, { height: 400, yaxis: { title: "t CO2e" }, legend: { orientation: "h", y: -0.22 }, margin: { l: 64, r: 16, t: 10, b: 90 } });
  const g = main.find((r) => r.year === +st.target_year);
  if (g) $("#gap").innerHTML = esc(t("targets.gap", { year: st.target_year, gap: (g["gap_" + b] > 0 ? "+" : "") + tons(g["gap_" + b]), verdict: g["gap_" + b] <= 0 ? t("targets.on_track") : t("targets.more_action") }));
  // ---- MACC for the selected boundary, with the gap to the SBTi target as a "reduction needed" line
  const bc = { scope12: COLORS.s1, scope3_nonflag: COLORS.nonflag, flag: COLORS.flag };
  const gapRow = main.find((r) => r.year === +st.target_year) || main[main.length - 1];
  const need = Math.max(0, gapRow["bau_" + b] - gapRow["target_" + b]);
  let cum = 0, closed = false;
  const mrows = d.macc.filter((m) => m.boundary === b).sort((a, z) => a.cost_eur_per_t_scenario - z.cost_eur_per_t_scenario).map((m) => {
    const x0 = cum; cum += m.abatement_tco2e;
    const inRoute = !closed; if (cum >= need) closed = true;
    return { ...m, x0, x1: cum, cum, net_cost: m.cost_eur_per_t_scenario * m.abatement_tco2e, in_route: need > 0 && inRoute };
  });
  // ---- "How to reach the target": one stacked bar (cheapest action first) + numbered steps
  const band = (c) => (c < 0 ? "#2f6f4f" : c < 50 ? "#7aa95c" : c <= 150 ? "#c9a227" : "#b23a48");
  const bands = [["macc.c_save", "#2f6f4f"], ["macc.c_low", "#7aa95c"], ["macc.c_mid", "#c9a227"], ["macc.c_high", "#b23a48"]];
  const span = Math.max(cum, need);
  const steps = mrows.map((m) => ({ type: "bar", orientation: "h", y: [""], x: [m.abatement_tco2e], showlegend: false,
    marker: { color: band(m.cost_eur_per_t_scenario), line: { color: "#fff", width: 2 } }, text: [m.abatement_tco2e > span * 0.04 ? m.lever_id : ""],
    textposition: "inside", insidetextanchor: "middle", textfont: { color: "#fff", size: 12 },
    hovertext: [`<b>${m.lever_id} ${esc(lev(m.lever_id, m.name))}</b><br>${tons(m.abatement_tco2e)} / a · ${eur(m.cost_eur_per_t_scenario)} / t`], hoverinfo: "text" }));
  for (const [k, c] of bands) steps.push({ type: "bar", orientation: "h", y: [""], x: [0], name: t(k), marker: { color: c }, hoverinfo: "skip" });
  plot($("#macc"), steps, { barmode: "stack", height: 220, xaxis: { title: t("macc.axis"), range: [0, span * 1.06] }, yaxis: { visible: false },
    legend: { orientation: "h", y: -0.7, traceorder: "normal" }, margin: { l: 20, r: 20, t: 36, b: 100 }, noControls: true,
    shapes: need > 0 ? [{ type: "line", x0: need, x1: need, yref: "paper", y0: -0.05, y1: 1.1, line: { color: COLORS.target, width: 3, dash: "dash" } }] : [],
    annotations: need > 0 ? [{ x: need, yref: "paper", y: 1.1, text: `<b>${t("macc.target_line", { year: st.target_year, t: tons(need) })}</b>`, showarrow: false,
      xanchor: need > span * 0.6 ? "right" : "left", yanchor: "bottom", font: { color: COLORS.target, size: 12 } }] : [] });
  const cheapest = mrows.filter((m) => m.in_route);
  const bname = B[b];
  let summary;
  if (need <= 0) summary = t("macc.no_gap", { boundary: bname, year: st.target_year });
  else if (cum < need) summary = t("macc.short", { boundary: bname, t: tons(cum), short: tons(need - cum), year: st.target_year });
  else summary = t("macc.summary", { boundary: bname, gap: tons(need), year: st.target_year, list: cheapest.map((m) => `${m.lever_id} ${lev(m.lever_id, m.name)}`).join(", "),
    t: tons(cheapest.reduce((a, m) => a + m.abatement_tco2e, 0)), cost: eur(cheapest.reduce((a, m) => a + m.net_cost, 0)) });
  const stepTxt = (m) => (m.cost_eur_per_t_scenario < 0 ? t("macc.step_save", { t: tons(m.abatement_tco2e), eur: eur(-m.net_cost) })
    : t("macc.step_cost", { t: tons(m.abatement_tco2e), eur: eur(m.net_cost), pt: eur(m.cost_eur_per_t_scenario) }));
  let stepsHtml = `<h2 class="section">${esc(t("macc.steps"))}</h2><ol class="steps">`, optional = false;
  mrows.forEach((m, i) => {
    if (need > 0 && !m.in_route && !optional) { optional = true; stepsHtml += `</ol><p class="muted">${esc(t("macc.optional"))}</p><ol class="steps" start="${i + 1}">`; }
    const reachedHere = need > 0 && m.in_route && m.cum >= need;
    stepsHtml += `<li class="${m.in_route || need === 0 ? "" : "opt"}"><span class="dot" style="background:${band(m.cost_eur_per_t_scenario)}"></span>
      <strong>${esc(lev(m.lever_id, m.name))}</strong> <span class="muted">(${m.lever_id} · ${esc(own(m.owner))})</span><br>${esc(stepTxt(m))}
      ${reachedHere ? `<span class="reached">${esc(t("macc.reached"))}</span>` : ""}</li>`;
  });
  stepsHtml += "</ol>";
  $("#macc").insertAdjacentHTML("afterend", `<p class="macc-summary"><strong>${esc(summary)}</strong></p>` + stepsHtml);
  $("#macc").insertAdjacentHTML("beforebegin", help("help.macc"));
  $("#lv").innerHTML = help("help.levers") + table([{ k: "lever_id", label: "ID" }, { k: "name", label: t("col.lever"), f: (x, r) => esc(lev(r.lever_id, x)) },
    { k: "kind", label: t("col.kind"), f: (x) => esc(lbl("kind", x)) }, { k: "boundary", label: t("targets.boundary"), f: (x) => esc(B[x] || x) },
    { k: "abatement_tco2e", label: t("col.abatement"), num: true }, { k: "cost_eur_per_t", label: t("col.cost_t"), num: true }, { k: "confidence", label: t("overview.confidence"), f: (x) => esc(conf(x)) },
    { k: "owner", label: t("overview.owner"), f: (x) => esc(own(x)) }, { k: "abatement_basis", label: t("col.basis"), f: (x) => `<span class="muted">${esc(x)}</span>` }], d.levers, { max: 420 });
}

/* ---------------------------------------------------------------- climate */
async function renderClimate(v) {
  const st = Object.assign({ crop: "turnip_rape", region: "FI_SW", icrop: "potato", iscen: "ssp585" }, S.cf || {});
  const [d, s, irr] = await Promise.all([api("/api/climate"), api("/api/climate/series" + q({ crop: st.crop, region: st.region })), api("/api/climate/irrigation" + q({ crop: st.icrop, scenario: st.iscen }))]);
  const R = { FI_SW: "FI", SE_SKANE: "SE" };
  v.innerHTML = `<h1>${esc(t("climate.title"))}</h1>
    <div class="card"><div class="filters"><label>${esc(t("common.crop"))}<select id="cc">${["turnip_rape", "pea", "potato", "carrot", "spinach"].map((c) => `<option value="${c}" ${c === st.crop ? "selected" : ""}>${esc(lbl("crop", c))}</option>`).join("")}</select></label>
      <label>${esc(t("common.region"))}<select id="cr">${s.regions.map((r) => `<option value="${r}" ${r === st.region ? "selected" : ""}>${esc(lbl("region", r))}</option>`).join("")}</select></label></div>
      <h2>${esc(t("climate.series"))}</h2><div id="ser" class="chart"></div></div>
    <div class="grid cols-2e section"><div class="card"><h2>${esc(t("climate.risk"))}</h2><div id="risk"></div></div><div class="card"><h2>${esc(t("climate.eur"))}</h2><div id="eur"></div></div></div>
    <div class="grid cols-2e section"><div class="card"><h2>${esc(t("climate.irrigation"))}</h2><div class="filters">
      <label>${esc(t("common.crop"))}<select id="ic">${["potato", "carrot", "pea", "spinach"].map((c) => `<option value="${c}" ${c === st.icrop ? "selected" : ""}>${esc(lbl("crop", c))}</option>`).join("")}</select></label>
      <label>${esc(t("common.scenario"))}<select id="is">${["ssp585", "ssp245", "ssp126"].map((c) => `<option value="${c}" ${c === st.iscen ? "selected" : ""}>${esc(lbl("scen", c))}</option>`).join("")}</select></label></div>
      ${kpi(lbl("crop", irr.crop) + " · " + lbl("scen", irr.scenario), (irr.gain_pct >= 0 ? "+" : "") + pct(irr.gain_pct, 1), tons(irr.yield_gain_t_per_yr).replace("t", "t / a"))}</div>
      <div class="card"><h2>${esc(t("climate.backtest"))}</h2><div id="bt"></div></div></div>`;
  const re = () => { S.cf = { crop: $("#cc").value, region: $("#cr").value, icrop: $("#ic").value, iscen: $("#is").value }; route(); };
  ["#cc", "#cr", "#ic", "#is"].forEach((x) => $(x).onchange = re);
  const col = { historical: "#555", ssp126: COLORS.s1, ssp245: COLORS.s3, ssp585: COLORS.target }, tr = [];
  Object.entries(s.series).forEach(([sc, g]) => {
    tr.push({ x: g.year, y: g.y, name: sc, mode: "lines", line: { color: col[sc], width: 1 }, opacity: .4, showlegend: false });
    tr.push({ x: g.year, y: g.smooth, name: lbl("scen", sc), mode: "lines", line: { color: col[sc], width: 3 } });
  });
  plot($("#ser"), tr, { height: 360, yaxis: { title: "t/ha" }, shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: s.bad_year, y1: s.bad_year, line: { color: COLORS.target, dash: "dash" } }],
    annotations: [{ xref: "paper", x: 0, y: s.bad_year, text: t("climate.bad_year"), showarrow: false, yanchor: "bottom", xanchor: "left", font: { color: COLORS.target, size: 11 } }] });
  const cropC = { k: "crop", label: t("col.crop"), f: (x) => esc(lbl("crop", x)) }, regC = { k: "region", label: t("col.region"), f: (x) => esc(lbl("region", x)) },
    scenC = { k: "scenario", label: t("col.scenario"), f: (x) => esc(lbl("scen", x)) };
  $("#risk").innerHTML = help("help.risk") + table([cropC, regC, scenC, { k: "mean_yield_change_pct", label: t("col.yield_change"), num: true, d: 1 },
    { k: "p_bad_year", label: t("col.p_bad"), f: (x) => (x === null || x === undefined ? "–" : esc(pct(100 * x))), num: true }], d.risk, { max: 380 });
  $("#eur").innerHTML = help("help.eur") + table([cropC, regC, scenC, { k: "expected_shortfall_t", label: t("col.shortfall"), num: true },
    { k: "expected_cost_eur", label: t("col.cost"), num: true }, { k: "p95_cost_eur", label: t("col.cost95"), num: true }], d.eur, { max: 380 });
  $("#bt").innerHTML = help("help.backtest") + table([cropC, regC,
    { k: "model_mape_yearly", label: t("col.model_mape"), f: (x) => (x === null || x === undefined ? "–" : esc(pct(100 * x, 1))), num: true }, { k: "clim_mape_yearly", label: t("col.base_mape"), f: (x) => (x === null || x === undefined ? "–" : esc(pct(100 * x, 1))), num: true },
    { k: "model_nrmse", label: t("col.model_nrmse"), f: (x) => (x === null || x === undefined ? "–" : esc(pct(100 * x, 1))), num: true }, { k: "clim_nrmse", label: t("col.base_nrmse"), f: (x) => (x === null || x === undefined ? "–" : esc(pct(100 * x, 1))), num: true }], d.backtest);
}

/* ---------------------------------------------------------------- nature */
async function renderNature(v) {
  const [d, m] = await Promise.all([api("/api/nature"), api("/api/nature/map")]);
  const region = S.nRegion || "FI_SW";
  v.innerHTML = `<h1>${esc(t("nature.title"))}</h1>
    <div class="card"><div class="filters"><label>${esc(t("common.region"))}<select id="nr">${["FI_SW", "SE_SKANE"].map((r) => `<option value="${r}" ${region === r ? "selected" : ""}>${esc(lbl("region", r))}</option>`).join("")}</select></label></div>
      <h2>${esc(t("nature.map"))}</h2><div id="map" class="chart" style="height:480px"></div>
      <div class="ev">P: ±${num(100 * (10 ** d.calibration.p.dev_mae_log10 - 1))} % (dev catchments, in-sample) · delivery length ${num(d.calibration.p.delivery_length_m)} m</div></div>
    <div class="card section"><h2>${esc(t("nature.priority"))}</h2><div id="pr"></div></div>
    <div class="card section"><h2>${esc(t("nature.hotspots"))}</h2><div id="hs"></div></div>
    <div class="card section"><h2>${esc(t("nature.materiality"))}</h2><div id="d2"></div></div>`;
  $("#nr").onchange = (e) => { S.nRegion = e.target.value; route(); };
  const feats = m.catchments.features.filter((f) => f.properties.region === region);
  const pts = m.parcels.filter((p) => (region === "FI_SW" ? p.catchment_id.startsWith("CA") : p.catchment_id.startsWith("CS")));
  const lat = pts.reduce((a, p) => a + p.lat, 0) / pts.length, lon = pts.reduce((a, p) => a + p.lon, 0) / pts.length;
  plot($("#map"), [
    { type: "choroplethmap", geojson: { type: "FeatureCollection", features: feats }, locations: feats.map((f) => f.id), z: feats.map((f) => f.properties.priority_score),
      colorscale: [[0, "#f3e7e9"], [1, "#b23a48"]], marker: { opacity: .55, line: { width: 1, color: "#666" } }, colorbar: { title: "priority", thickness: 10 },
      text: feats.map((f) => `${f.id} · rank ${f.properties.priority_rank} · ${f.properties.ecological_status_class} ${f.properties.receiving_water_type}<br>P ${num(f.properties.p_kg)} kg`), hoverinfo: "text" },
    { type: "scattermap", lat: pts.map((p) => p.lat), lon: pts.map((p) => p.lon), mode: "markers", marker: { size: pts.map((p) => 4 + Math.sqrt(p.p_kg) * 2.2), color: COLORS.s1, opacity: .75 },
      text: pts.map((p) => `${p.parcel_id} · ${lbl("crop", p.crop)} · ${num(p.area_ha, 1)} ha · P ${num(p.p_kg, 1)} kg`), hoverinfo: "text", showlegend: false },
  ], { map: { style: "carto-positron", center: { lat, lon }, zoom: 8.2 }, height: 480, margin: { l: 0, r: 0, t: 0, b: 0 } }).catch(() => {
    // No WebGL (e.g. remote desktop, headless): draw catchments and parcels as plain SVG instead.
    const outlines = feats.map((f) => { const ring = (f.geometry.type === "Polygon" ? f.geometry.coordinates : f.geometry.coordinates[0])[0];
      const sc = f.properties.priority_score / Math.max(...feats.map((g) => g.properties.priority_score), 1e-9);
      return { type: "scatter", mode: "lines", x: ring.map((c) => c[0]), y: ring.map((c) => c[1]), fill: "toself", fillcolor: `rgba(178,58,72,${0.08 + 0.5 * sc})`,
        line: { color: "#777", width: 1 }, hoverinfo: "text", text: `${f.id} · rank ${f.properties.priority_rank}`, showlegend: false }; });
    $("#map").innerHTML = "";
    plot($("#map"), [...outlines, { type: "scatter", mode: "markers", x: pts.map((p) => p.lon), y: pts.map((p) => p.lat),
      marker: { size: pts.map((p) => 3 + Math.sqrt(p.p_kg) * 2), color: COLORS.s1, opacity: .7 }, text: pts.map((p) => `${p.parcel_id} · P ${num(p.p_kg, 1)} kg`), hoverinfo: "text", showlegend: false }],
    { height: 480, xaxis: { visible: false }, yaxis: { visible: false, scaleanchor: "x", scaleratio: 1 / Math.cos(lat * Math.PI / 180) }, margin: { l: 0, r: 0, t: 0, b: 0 } });
  });
  $("#pr").innerHTML = help("help.priority") + table([{ k: "priority_rank", label: t("col.rank"), num: true }, { k: "catchment_id", label: t("col.catchment") },
    { k: "receiving_water_type", label: t("col.water"), f: (x) => esc(lbl("water", x)) }, { k: "ecological_status_class", label: t("col.status"), f: (x) => esc(lbl("status", x)) },
    { k: "apetit_area_ha", label: t("col.contract_ha"), num: true }, { k: "apetit_p_to_water_kg", label: t("col.p_kg"), num: true }, { k: "apetit_n_to_water_kg", label: t("col.n_kg"), num: true },
    { k: "state_weight", label: t("col.state_w"), num: true, d: 1 }, { k: "p_sensitivity", label: t("col.p_sens"), num: true, d: 2 }, { k: "priority_score", label: t("col.priority"), num: true, d: 2 }], d.priority, { max: 360 });
  $("#hs").innerHTML = help("help.hotspots") + table([{ k: "parcel_id", label: t("col.parcel") }, { k: "catchment_id", label: t("col.catchment") }, { k: "crop", label: t("col.crop"), f: (x) => esc(lbl("crop", x)) },
    { k: "area_ha", label: t("col.area_ha"), num: true, d: 1 }, { k: "p_kg", label: t("col.p_kg"), num: true, d: 1 }, { k: "dist_to_water_m", label: t("col.dist_m"), num: true },
    { k: "p_test_filled", label: t("col.soil_p"), num: true, d: 1 }, { k: "action", label: t("col.action"), f: (x) => esc(act(x)) }], d.hotspots, { max: 340 });
  $("#d2").innerHTML = help("help.materiality") + table([{ k: "material", label: t("col.material") }, { k: "origin_country", label: t("col.origin") }, { k: "tco2e", label: t("col.emissions"), num: true },
    { k: "materiality_score", label: t("col.materiality"), num: true, d: 1 }, { k: "why", label: t("col.why"), f: (x) => `<span class="muted">${esc(x)}</span>` }], d.materiality, { max: 360 });
}

/* ---------------------------------------------------------------- live context */
async function renderLive(v) {
  const d = await api("/api/live");
  if (!d.alive) { v.innerHTML = `<h1>${esc(t("live.title"))}</h1><div class="card"><p>${esc(t("live.offline"))}</p></div>`; return; }
  const st = d.status;
  v.innerHTML = `<h1>${esc(t("live.title"))}</h1><p class="lead">Orion-LD ${esc(st.version)} · <code>${esc(st.context)}</code></p>
    <h2>${esc(t("live.entities"))}</h2><div class="grid kpis">${Object.entries(st.entities).map(([k, n]) => kpi(k, num(n))).join("")}</div>
    <div class="card"><h2>${esc(t("live.subscriptions"))}</h2><div id="subs"></div></div>
    <div class="card section"><h2>${esc(t("live.issues"))}</h2><div id="li"></div>
      <div class="filters section"><label>${esc(t("quality.reviewer"))}<input type="text" id="lrev" value="${esc(localStorage.getItem("juuri.reviewer") || "")}"></label>
      <button class="btn" id="lap">${esc(t("quality.approve"))}</button><button class="btn danger" id="lrj">${esc(t("quality.reject"))}</button></div></div>
    <div class="card section"><h2>${esc(t("live.drought"))}</h2><p class="muted">${esc(d.last_observation || "")}</p><div id="dr"></div>
      <div class="section"><button class="btn" id="adv">▶ ${esc(t("live.advance"))}</button> <button class="btn ghost" id="lreset">↺ ${esc(t("live.reset"))}</button></div></div>`;
  $("#subs").innerHTML = table([{ k: "id", label: t("col.subscription") }, { k: "status", label: t("col.sub_status") }, { k: "timesSent", label: t("col.sent"), num: true },
    { k: "lastSuccess", label: t("col.last"), f: (x) => esc((x || "–").replace("T", " ").slice(0, 19)) }], st.subscriptions);
  $("#li").innerHTML = table([{ k: "issueType", label: t("quality.col.type"), f: (x) => esc(lbl("issue", x)) }, { k: "recordKey", label: t("quality.col.record") },
    { k: "observedValue", label: t("quality.col.old"), num: true, d: 1 }, { k: "proposedValue", label: t("quality.col.new"), num: true, d: 1 },
    { k: "reviewStatus", label: t("quality.col.status"), f: statusPill }, { k: "explanation", label: t("quality.col.why"), f: (x) => `<span class="muted">${esc(x)}</span>` }], d.issues, { select: true });
  $("#dr").innerHTML = help("help.drought") + table([{ k: "region", label: t("col.region"), f: (x) => esc(lbl("region", x)) }, { k: "crop", label: t("col.crop"), f: (x) => esc(lbl("crop", x)) },
    { k: "parcels", label: t("col.parcels"), num: true }, { k: "area_ha", label: t("col.area_ha"), num: true }, { k: "impact_pct", label: t("col.impact"), num: true, d: 1 },
    { k: "moderate", label: t("col.moderate"), num: true }, { k: "high", label: t("col.high"), num: true }, { k: "deficit_mm", label: t("col.deficit"), num: true }], d.drought);
  const decide = async (decision) => {
    const reviewer = $("#lrev").value.trim();
    if (!reviewer) return toast(t("quality.need_reviewer"));
    const sel = [...v.querySelectorAll("#li [data-sel]:checked")].map((c) => d.issues[+c.dataset.sel]);
    if (!sel.length) return;
    await api("/api/live/decision", { method: "POST", body: JSON.stringify({ ids: sel.map((x) => x.id), record_keys: sel.map((x) => x.recordKey), decision, reviewer }) });
    toast(t("quality.saved", { n: sel.length })); route();
  };
  $("#lap").onclick = () => decide("approved"); $("#lrj").onclick = () => decide("rejected");
  const busy = (on) => { $("#adv").disabled = on; $("#lreset").disabled = on; $("#adv").textContent = (on ? "⏳ " : "▶ ") + t("live.advance"); };
  $("#adv").onclick = async () => {
    busy(true);
    try {
      const r = await api("/api/live/advance?days=7", { method: "POST" });
      const month = r.meter_month ? new Intl.DateTimeFormat(S.lang, { month: "long", year: "numeric" }).format(new Date(2025, r.meter_month - 1, 1)) : "–";
      await route();
      toast(r.done ? t("live.season_over") : t("live.advanced", { start: r.start, end: r.end, month, issues: r.new_issues }));
    } catch (e) { toast(e.message); busy(false); }
  };
  $("#lreset").onclick = async () => {
    busy(true);
    try { await api("/api/live/reset", { method: "POST" }); await route(); toast(t("live.reset_done")); } catch (e) { toast(e.message); busy(false); }
  };
}

/* ---------------------------------------------------------------- method */
async function renderMethod(v) {
  const [d, st] = await Promise.all([api("/api/method"), api("/api/store")]);
  v.innerHTML = `<h1>${esc(t("method.title"))}</h1>${d.scores ? `<div class="card"><h2>${esc(t("method.scores"))}</h2><pre>${esc(JSON.stringify(d.scores, null, 1))}</pre></div>` : ""}
    <div class="card section"><h2>${esc(t("store.title"))}</h2>${help("store.help")}
      <h2 class="section">${esc(t("store.deliveries"))}</h2>${st.deliveries.length ? table([{ k: "delivery_id", label: t("col.delivery") },
        { k: "received_at", label: t("col.received"), f: (x) => esc(x.replace("T", " ")) }, { k: "source", label: t("col.source_sys") },
        { k: "files", label: t("col.files") }, { k: "records", label: t("col.rows"), num: true }], st.deliveries) : `<p class="muted">${esc(t("store.none_d"))}</p>`}
      <h2 class="section">${esc(t("store.snapshots"))}</h2>${st.snapshots.length ? table([{ k: "label", label: t("col.label") },
        { k: "created", label: t("col.created"), f: (x) => esc(x.replace("T", " ")) }, { k: "year", label: t("col.year") },
        { k: "status", label: t("col.rstatus") }, { k: "scope1", label: t("col.s1"), num: true }, { k: "scope2_market", label: t("col.s2"), num: true },
        { k: "scope3", label: t("col.s3"), num: true }], st.snapshots) : `<p class="muted">${esc(t("store.none_s"))}</p>`}</div>
    <div class="card section method-body">${DOMPurify.sanitize(d.html)}</div><p class="ev"><a href="/api/docs" target="_blank">API (OpenAPI)</a></p>`;
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  $("#policySel").value = S.policy;
  $("#policySel").onchange = (e) => { S.policy = e.target.value; localStorage.setItem("juuri.policy", S.policy); refreshMeta(); route(); };
  document.querySelectorAll("#langSwitch button").forEach((b) => b.onclick = async () => { await loadLang(b.dataset.lang); route(); const dc = $("#drawerChat"); if (dc.innerHTML) chatWidget(dc, true); });
  $("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");
  $("#chatFab").onclick = () => { const dr = $("#drawer"); dr.classList.toggle("open"); if (dr.classList.contains("open")) chatWidget($("#drawerChat"), true); };
  $("#drawerClose").onclick = () => $("#drawer").classList.remove("open");
  $("#drawerExpand").onclick = () => { location.hash = "#/assistant"; };
  window.addEventListener("hashchange", route);
  await loadLang(S.lang);
  await refreshMeta();
  route();
}
boot();
