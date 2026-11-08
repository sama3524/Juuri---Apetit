"""Data history: append-only deliveries and frozen "as reported" snapshots.

Raw data is never edited or deleted. The data package is the *baseline*; every new export is
ingested as a *delivery* under ``raw/<delivery_id>/`` with a manifest (source, checksums, rows).
Juuri reads the effective data = baseline overlaid by all deliveries in order: for tables with a
natural key the newest delivery wins record by record; other files are replaced whole by the newest
copy. Every overlaid row carries ``_delivery`` so lineage shows which delivery it came from.

When figures are published, ``freeze`` writes a read-only snapshot (results + manifest with the
data deliveries, decisions fingerprint, factor-library checksum and code version), and ``compare``
shows what has changed since, flagging the 5 % significance threshold for restatement.

    python -m juuri ingest <file-or-folder> --source "ERP export"   # validate + store a delivery
    python -m juuri deliveries                                      # list deliveries
    python -m juuri freeze 2025-annual-report [--year 2025] [--policy strict|provisional]
    python -m juuri snapshots                                       # list snapshots
    python -m juuri compare 2025-annual-report                       # reported vs recalculated now
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pandas as pd

from . import config as C

RAW_DIR = Path(os.environ.get("JUURI_RAW", C.REPO_ROOT / "raw"))
SNAP_DIR = Path(os.environ.get("JUURI_SNAPSHOTS", C.REPO_ROOT / "snapshots"))
SIGNIFICANCE = 0.05   # GHG Protocol / SBTi significance threshold for base-year recalculation

# relative path under data/ -> (key in raw(), natural key columns or None for whole-file replace, read options)
TABLES = {
    "02_raw_feeds/erp_purchase_lines_2021_2025.csv": ("erp", ["doc_no", "line_no"], dict(dtype={"doc_no": str, "reference_doc": str})),
    "02_raw_feeds/energy_meter_readings.csv": ("energy_meter", ["site_id", "year", "month", "carrier"], {}),
    "02_raw_feeds/fuel_deliveries.csv": ("fuel_deliveries", ["delivery_id"], {}),
    "02_raw_feeds/fuel_stock_changes.csv": ("fuel_stock", ["site_id", "carrier", "year"], {}),
    "02_raw_feeds/goo_cancellations.csv": ("goo", ["site_id", "year"], {}),
    "02_raw_feeds/goo_registry_statement.csv": ("goo_registry", ["cancellation_id"], {}),
    "02_raw_feeds/refrigerant_service_log.csv": ("refrigerants", ["event_id"], {}),
    "02_raw_feeds/waste_transfer_notes.csv": ("waste", ["transfer_note"], {}),
    "02_raw_feeds/travel_bookings.csv": ("travel", ["booking_id"], {}),
    "02_raw_feeds/commuting_survey_2025.csv": ("commuting", ["response_id"], {}),
    "02_raw_feeds/headcount.csv": ("headcount", ["year", "site_id"], {}),
    "02_raw_feeds/shipments.csv": ("shipments", ["shipment_id"], {}),
    "02_raw_feeds/sales_volumes.csv": ("sales", ["year", "month", "product_group", "site_id", "market"], {}),
    "02_raw_feeds/water_balance.csv": ("water", ["site_id", "year", "flow", "source_or_destination"], {}),
    "02_raw_feeds/wastewater_third_party.csv": ("wastewater", ["year", "site_id"], {}),
    "02_raw_feeds/heat_supplier_factors.csv": ("heat_suppliers", ["site_id", "year"], {}),
    "02_raw_feeds/frozen_warehousing.csv": ("warehousing", ["year"], {}),
    "02_raw_feeds/capex_projects.csv": ("capex", ["year", "site_id", "project"], {}),
    "03_farming/farm_survey_fi.csv": ("farm_survey", ["survey_id"], {}),
    "03_farming/parcel_crop_history_and_plan.csv": ("crop_history", ["parcel_id", "year"], {}),
    "03_farming/current_calculator_footprints.csv": ("current_calc", ["farm_id", "crop", "year"], {}),
    "03_farming/foodhills_odlingsdata_se.csv": ("foodhills", ["odlar_id", "skördeår", "gröda"], dict(sep=";", decimal=",")),
    "01_master/farm_registry.csv": ("farm_registry", ["farm_id"], {}),
    "01_master/parcels.csv": ("parcels", ["parcel_id"], {}),
    "01_master/suppliers.csv": ("suppliers", ["supplier_id"], {}),
    "01_master/materials.csv": ("materials", ["material_id"], {}),
    "01_master/emission_factor_library.csv": ("ef_library", ["ef_id"], {}),
    "01_master/sites.csv": ("sites", ["site_id"], {}),
    "01_master/climate_grid_cells.csv": ("climate_cells", ["cell_id"], {}),
    "06_nature/catchments.csv": ("catchments", ["catchment_id"], {}),
    "06_nature/monitored_catchment_loads.csv": ("catchment_loads", ["catchment_id", "year"], {}),
    "06_nature/origin_state_of_nature_indicative.csv": ("origin_nature", None, {}),
    "05_scenarios/transition_scenarios.csv": ("scenarios", None, {}),
    "05_scenarios/decarbonisation_levers.csv": ("levers", None, {}),
    "05_scenarios/structural_events.csv": ("events", None, {}),
    "05_scenarios/contract_prices_and_replacement_premiums.csv": ("prices", None, {}),
    "05_scenarios/published_inventory_history.csv": ("published", None, {}),
    "05_scenarios/draft_report_summary_2025.csv": ("draft_report", None, {}),
    "07_frameworks/source_registry.csv": ("source_registry", None, {}),
}
# files used by path (weather, geometry, documents): the newest delivered copy replaces the baseline file
FILES = ["04_climate/weather_daily_1991_2060.parquet", "04_climate/weather_monthly_1991_2060.csv",
         "04_climate/climate_change_signals.csv", "06_nature/catchments.geojson", "06_nature/parcels.geojson",
         "07_frameworks/framework_digest.md"]
PCF_DIR = "02_raw_feeds/supplier_pcfs"


# ------------------------------------------------------------------------------ deliveries
def deliveries() -> list[dict]:
    """All deliveries in ingestion order (oldest first)."""
    if not RAW_DIR.exists():
        return []
    out = []
    for m in sorted(RAW_DIR.glob("*/manifest.json")):
        out.append(json.loads(m.read_text(encoding="utf-8")))
    return sorted(out, key=lambda d: (d["received_at"], d["delivery_id"]))


def fingerprint() -> str:
    """Changes whenever a delivery is added: used to invalidate caches."""
    h = hashlib.sha256()
    for d in deliveries():
        h.update(d["delivery_id"].encode())
        for f in d["files"]:
            h.update(f["sha256"].encode())
    return h.hexdigest()[:16]


_SEEN = {"fp": None}


def refresh() -> bool:
    """Clear every derived cache when a new delivery has arrived. Returns True if it did."""
    fp = fingerprint()
    if _SEEN["fp"] is None:
        _SEEN["fp"] = fp
        return False
    if fp == _SEEN["fp"]:
        return False
    _SEEN["fp"] = fp
    import importlib
    for mod, names in {"juuri.inventory": ["_canonical"], "juuri.climate": ["weather_features", "parcels", "training_data", "model",
                       "climate_yields", "backtest"], "juuri.nature": ["parcel_risk", "calibration", "catchments_geojson"],
                       "juuri.copilot": ["answer_e", "_chunks"], "juuri.fiware": ["_weather_2026"]}.items():
        m = importlib.import_module(mod)
        for n in names:
            fn = getattr(m, n, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
    try:
        from . import api
        api._LINEAGE.clear()
    except Exception:  # noqa: BLE001 - api not loaded (CLI use)
        pass
    return True


def path(rel: str) -> Path:
    """Newest copy of a file used by path (baseline if no delivery contains it)."""
    for d in reversed(deliveries()):
        p = RAW_DIR / d["delivery_id"] / "data" / rel
        if p.exists():
            return p
    return C.DATA / rel


def _read(p: Path, opts: dict) -> pd.DataFrame:
    return pd.read_csv(p, **opts)


def apply_deliveries(r: dict) -> dict:
    """Overlay deliveries onto the baseline tables in ``r`` (in place)."""
    ds = deliveries()
    if not ds:
        return r
    for rel, (name, key, opts) in TABLES.items():
        parts = []
        for d in ds:
            p = RAW_DIR / d["delivery_id"] / "data" / rel
            if p.exists():
                parts.append(_read(p, opts).assign(_delivery=d["delivery_id"]))
        if not parts or name not in r:
            continue
        base = r[name].assign(_delivery="baseline") if "_delivery" not in r[name] else r[name]
        if key is None:
            r[name] = parts[-1]
            continue
        allr = pd.concat([base] + parts, ignore_index=True)
        rank = {"baseline": 0, **{d["delivery_id"]: i + 1 for i, d in enumerate(ds)}}
        allr["_rank"] = allr["_delivery"].map(rank)
        newest = allr.groupby(key, dropna=False)["_rank"].transform("max")
        r[name] = allr[allr["_rank"] == newest].drop(columns="_rank").reset_index(drop=True)
    # supplier PCFs (one JSON per supplier x material x year): newest file of the same name wins
    pcf_files = {}
    for d in ds:
        for f in (RAW_DIR / d["delivery_id"] / "data" / PCF_DIR).glob("*.json"):
            pcf_files[f.name] = (f, d["delivery_id"])
    if pcf_files:
        rows = []
        for name, (f, did) in pcf_files.items():
            js = json.loads(f.read_text())["pcf"]
            sup, mat, year = Path(name).stem.split("_")
            rows.append(dict(file=name, supplier_id=sup, material_id=mat, year=int(year), value=float(js["pCfExcludingBiogenic"]),
                             declared_unit=js["declaredUnit"], ref_start=js["referencePeriodStart"][:10],
                             ref_end=js["referencePeriodEnd"][:10], _delivery=did))
        p = r["pcfs"]
        p = p[~p.file.isin(pcf_files)]
        r["pcfs"] = pd.concat([p.assign(_delivery="baseline") if "_delivery" not in p else p, pd.DataFrame(rows)], ignore_index=True)
    return r


def _known(name: str) -> str | None:
    """Map an incoming file name to its path under data/."""
    for rel in list(TABLES) + FILES:
        if Path(rel).name == name:
            return rel
    if re.fullmatch(r"SUP\d{3}_M\d{3}_\d{4}\.json", name):
        return f"{PCF_DIR}/{name}"
    return None


def validate(src: Path, rel: str) -> list[str]:
    """Problems that block ingestion (empty list = OK)."""
    if rel not in TABLES:
        return [] if src.stat().st_size > 0 else ["file is empty"]
    _, key, opts = TABLES[rel]
    try:
        new = _read(src, opts)
    except Exception as e:  # noqa: BLE001
        return [f"cannot be read as CSV: {e}"]
    base = _read(C.DATA / rel, opts)
    errs = []
    missing = [c for c in base.columns if c not in new.columns]
    if missing:
        errs.append(f"missing columns: {', '.join(missing)}")
    if new.empty:
        errs.append("no rows")
    if key and not missing and new.duplicated(key).any():
        errs.append(f"{int(new.duplicated(key).sum())} duplicate keys ({', '.join(key)}) inside the delivery")
    for c in base.columns:
        if c in new.columns and pd.api.types.is_numeric_dtype(base[c]) and not pd.api.types.is_bool_dtype(base[c]):
            bad = pd.to_numeric(new[c], errors="coerce").isna() & new[c].notna()
            if bad.mean() > 0.05:
                errs.append(f"column {c}: {int(bad.sum())} values are not numeric")
    return errs


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest(src: str | Path, source: str = "", delivery_id: str | None = None, note: str = "") -> dict:
    """Validate and store a delivery (a file or a folder of files). Raises ValueError if rejected."""
    src = Path(src)
    files = [p for p in (sorted(src.rglob("*")) if src.is_dir() else [src]) if p.is_file() and not p.name.startswith(".")]
    if not files:
        raise ValueError(f"no files found at {src}")
    plan, problems = [], {}
    for f in files:
        rel = _known(f.name)
        if rel is None:
            problems[f.name] = ["unknown file name: must match a file of the data package (e.g. energy_meter_readings.csv)"]
            continue
        errs = validate(f, rel)
        if errs:
            problems[f.name] = errs
        else:
            plan.append((f, rel))
    if problems:
        raise ValueError("delivery rejected:\n" + "\n".join(f"  {k}: {'; '.join(v)}" for k, v in problems.items()))
    now = dt.datetime.now()
    did = delivery_id or now.strftime("%Y%m%d-%H%M%S")
    did = re.sub(r"[^A-Za-z0-9_.-]", "-", did)
    target = RAW_DIR / did
    if target.exists():
        raise ValueError(f"delivery {did} already exists; deliveries are never overwritten")
    from .data import raw
    current = raw()
    entries = []
    for f, rel in plan:
        dst = target / "data" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        e = dict(path=rel, sha256=_sha(dst), bytes=dst.stat().st_size)
        if rel in TABLES:
            name, key, opts = TABLES[rel]
            new = _read(dst, opts)
            e["rows"] = len(new)
            if key and name in current:
                old_keys = set(map(tuple, current[name][key].astype(str).values))
                hits = sum(tuple(k) in old_keys for k in new[key].astype(str).values)
                e.update(updated_records=int(hits), new_records=int(len(new) - hits))
        entries.append(e)
    man = dict(delivery_id=did, received_at=now.isoformat(timespec="seconds"), source=source, note=note,
               received_by=os.environ.get("USER", ""), files=entries)
    (target / "manifest.json").write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    for p in target.rglob("*"):   # make the delivery read-only: raw data is never edited
        if p.is_file():
            p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return man


# ------------------------------------------------------------------------------ snapshots
def _git() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=C.REPO_ROOT, capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "juuri", "web"], cwd=C.REPO_ROOT,
                                    capture_output=True, text=True).stdout.strip())
        return dict(commit=commit, uncommitted_code_changes=dirty)
    except FileNotFoundError:
        return dict(commit=None)


def snapshots() -> list[dict]:
    if not SNAP_DIR.exists():
        return []
    return sorted((json.loads(m.read_text(encoding="utf-8")) for m in SNAP_DIR.glob("*/manifest.json")),
                  key=lambda s: s["created"], reverse=True)


def freeze(label: str, year: int = C.REPORT_YEAR, policy: str = "strict") -> dict:
    """Write a read-only snapshot of the reported figures and everything needed to reproduce them."""
    from . import inventory as INV, pathways as P, review as RV
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", label)
    out = SNAP_DIR / label
    if out.exists():
        raise ValueError(f"snapshot {label} already exists; snapshots are never overwritten")
    T = INV.canonical(policy)
    iss = T["_issues"]
    prov = int((iss.applied & (iss.applied_as == "provisional")).sum())
    out.mkdir(parents=True)
    summ = INV.summary(T, year)
    summ.to_csv(out / f"inventory_summary_{year}.csv", index=False)
    INV.all_years(policy).to_csv(out / "inventory_all_years.csv", index=False)
    INV.line_items(T, year).to_csv(out / f"line_items_{year}.csv", index=False)
    INV.lineage(T, year).to_csv(out / f"lineage_{year}.csv", index=False)
    INV.flag_split(T, year).to_csv(out / f"flag_split_{year}.csv", index=False)
    T["_audit"].to_csv(out / "audit_log.csv", index=False)
    iss.to_csv(out / "issues.csv", index=False)
    if C.DECISIONS_FILE.exists():
        shutil.copy2(C.DECISIONS_FILE, out / "decisions.csv")
    if year == C.REPORT_YEAR:
        P.targets(P.base_inventory(policy)).to_csv(out / "targets.csv", index=False)
    s = summ.set_index("category").tco2e
    dec = RV.load_decisions()
    pkg_manifest = C.PACKAGE_ROOT / "MANIFEST.json"
    man = dict(
        label=label, created=dt.datetime.now().isoformat(timespec="seconds"), reporting_year=year, policy=policy,
        status="DRAFT" if prov else "APPROVED", provisional_corrections=prov,
        totals=dict(scope1=float(s["scope1_total"]), scope2_location=float(s["scope2_location_total"]),
                    scope2_market=float(s["scope2_market_total"]), scope3=float(s["scope3_total"])),
        data=dict(package=json.loads(pkg_manifest.read_text()).get("version") if pkg_manifest.exists() else None,
                  deliveries=[dict(delivery_id=d["delivery_id"], received_at=d["received_at"], files=[f["path"] for f in d["files"]])
                              for d in deliveries()],
                  data_fingerprint=fingerprint(),
                  emission_factor_library_sha256=_sha(path("01_master/emission_factor_library.csv"))),
        review=dict(decisions_sha256=_sha(C.DECISIONS_FILE) if C.DECISIONS_FILE.exists() else None,
                    approved=int((dec.decision == "approved").sum()) if len(dec) else 0,
                    rejected=int((dec.decision == "rejected").sum()) if len(dec) else 0,
                    corrections_applied=int(iss.applied.sum())),
        code=dict(**_git(), python=platform.python_version(), pandas=pd.__version__),
        method="GHG Protocol Corporate & Scope 3 (operational control), IPCC AR6 GWP100; package reference calculator",
    )
    (out / "manifest.json").write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "README.md").write_text(
        f"# Snapshot {label}\n\nFrozen {man['created']} for reporting year {year} ({man['status']}, policy {policy}).\n\n"
        "Read-only record of the figures as reported. `manifest.json` lists the data deliveries, the reviewer decisions "
        "fingerprint, the emission-factor library checksum and the code version needed to reproduce them. "
        "Run `python -m juuri compare " + label + "` to see what has changed since.\n", encoding="utf-8")
    for p in out.iterdir():
        p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return man


def compare(label: str) -> dict:
    """Reported (snapshot) vs recalculated now with the same policy; flags changes above 5 %."""
    from . import inventory as INV
    snap = SNAP_DIR / label
    if not snap.exists():
        raise ValueError(f"no snapshot {label}")
    man = json.loads((snap / "manifest.json").read_text())
    year, policy = man["reporting_year"], man["policy"]
    old = pd.read_csv(snap / f"inventory_summary_{year}.csv")
    new = INV.summary(INV.canonical(policy), year)
    m = old.merge(new, on=["scope", "category"], how="outer", suffixes=("_reported", "_now")).fillna({"tco2e_reported": 0, "tco2e_now": 0})
    m["change_t"] = m.tco2e_now - m.tco2e_reported
    m["change_pct"] = 100 * m.change_t / m.tco2e_reported.where(m.tco2e_reported != 0)
    tot = m[m.scope == "total"].set_index("category")
    significant = [c for c, r in tot.iterrows() if c != "biogenic_co2_scope1" and r.tco2e_reported and abs(r.change_t / r.tco2e_reported) >= SIGNIFICANCE]
    since = [d for d in deliveries() if d["delivery_id"] not in {x["delivery_id"] for x in man["data"]["deliveries"]}]
    from . import review as RV
    dec_sha = _sha(C.DECISIONS_FILE) if C.DECISIONS_FILE.exists() else None
    return dict(snapshot=man, rows=m, significant=significant, new_deliveries=since,
                decisions_changed=dec_sha != man["review"]["decisions_sha256"], data_changed=fingerprint() != man["data"]["data_fingerprint"])


# ------------------------------------------------------------------------------ CLI
def main(argv: list[str]):
    ap = argparse.ArgumentParser(prog="python -m juuri")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ingest")
    a.add_argument("path")
    a.add_argument("--source", default="")
    a.add_argument("--id", dest="delivery_id")
    a.add_argument("--note", default="")
    sub.add_parser("deliveries")
    f = sub.add_parser("freeze")
    f.add_argument("label")
    f.add_argument("--year", type=int, default=C.REPORT_YEAR)
    f.add_argument("--policy", choices=["strict", "provisional"], default="strict")
    sub.add_parser("snapshots")
    c = sub.add_parser("compare")
    c.add_argument("label")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ingest":
            man = ingest(args.path, args.source, args.delivery_id, args.note)
            print(f"Delivery {man['delivery_id']} stored in {RAW_DIR / man['delivery_id']}")
            for e in man["files"]:
                extra = f" · {e.get('rows', '?')} rows ({e.get('updated_records', 0)} updated, {e.get('new_records', 0)} new)" if "rows" in e else ""
                print(f"  {e['path']}{extra}")
            print("Juuri picks it up automatically on the next page load or run.")
        elif args.cmd == "deliveries":
            ds = deliveries()
            print("baseline: data package" + ("" if ds else "  (no deliveries yet)"))
            for d in ds:
                print(f"{d['delivery_id']}  {d['received_at']}  {d['source'] or '-'}  " + ", ".join(Path(x['path']).name for x in d["files"]))
        elif args.cmd == "freeze":
            man = freeze(args.label, args.year, args.policy)
            print(f"Snapshot {man['label']} frozen ({man['status']}, policy {man['policy']}) in {SNAP_DIR / man['label']}")
            if man["status"] == "DRAFT":
                print(f"  note: {man['provisional_corrections']} corrections were still provisional (not approved by a reviewer).")
            elif man["policy"] == "strict" and man["review"]["approved"] == 0:
                print("  warning: no corrections approved yet, so these figures are computed from uncorrected raw data.")
        elif args.cmd == "snapshots":
            for s in snapshots():
                t = s["totals"]
                print(f"{s['label']}  {s['created']}  year {s['reporting_year']}  {s['status']}  "
                      f"S1 {t['scope1']:,.0f} · S2m {t['scope2_market']:,.0f} · S3 {t['scope3']:,.0f} t")
        elif args.cmd == "compare":
            res = compare(args.label)
            rows = res["rows"]
            tot = rows[rows.scope == "total"]
            print(f"Snapshot {args.label} (year {res['snapshot']['reporting_year']}, {res['snapshot']['status']}) vs now:")
            for r in tot.itertuples():
                pct = "" if pd.isna(r.change_pct) else f"{r.change_pct:+.1f} %"
                print(f"  {r.category:28s} {r.tco2e_reported:14,.1f} → {r.tco2e_now:14,.1f}  {pct}")
            print(f"  new deliveries since: {len(res['new_deliveries'])} · reviewer decisions changed: {res['decisions_changed']}")
            if res["significant"]:
                print("  ⚠ change ≥ 5 % in: " + ", ".join(res["significant"]) + " → a restatement / base-year recalculation may be required")
            else:
                print("  ✓ no change ≥ 5 %: no restatement required")
    except ValueError as e:
        sys.exit(str(e))
