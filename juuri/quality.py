"""Data-quality agent.

Each rule inspects one raw feed and *proposes* issues. An issue carries everything a reviewer
needs to accept or reject it: the record key, the field, the observed and proposed value, the rule
that fired, the evidence it used and a confidence. Nothing is changed in the raw data; corrections
are applied downstream (``canonical.py``) only for issues whose review status allows it.

Legitimate oddities are deliberately *not* flagged: credit notes (movement 122), vessel lots in
tonnes (uom TO), seasonal meter peaks and ammonia refrigeration systems.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .data import raw

C.use_reference_tools()
from ccn_tools import factors as F  # noqa: E402

COLUMNS = ["issue_id", "table", "record_key", "year", "issue_type", "confidence", "action", "field",
           "old_value", "new_value", "rule", "explanation", "evidence"]

ISSUE_TYPES = ["unit_error", "duplicate_record", "currency_error", "material_mismatch", "decimal_shift",
               "missing_record", "meter_spike", "wrong_gas_code", "certificate_mismatch", "soil_misreport",
               "outlier_implausible", "area_mismatch", "coordinate_swap", "pcf_unit_error", "pcf_stale_period",
               "total_mismatch", "missing_leg", "mode_mislabel"]


class _Collector:
    def __init__(self):
        self.rows = []

    def add(self, table, record_key, year, issue_type, confidence, action, field, old, new, rule, explanation, evidence=""):
        self.rows.append(dict(table=table, record_key=record_key, year=int(year), issue_type=issue_type,
                              confidence=round(float(confidence), 2), action=action, field=field,
                              old_value=None if old is None else str(old), new_value=None if new is None else str(new),
                              rule=rule, explanation=explanation, evidence=evidence))


# ---------------------------------------------------------------------------------------- ERP
def _erp(r, out: _Collector):
    e = r["erp"].copy()
    e["year"] = e.posting_date.str[:4].astype(int)
    e["date"] = pd.to_datetime(e.posting_date)
    sup = r["suppliers"].set_index("supplier_id")
    mats = r["materials"].set_index("material_id")
    goods = e[(e.uom != "EUR") & (e.movement_type == 101)].copy()
    goods["qty_kg"] = np.where(goods.uom == "TO", goods.quantity * 1000, goods.quantity)
    fx = goods.currency.map(F.FX_PER_EUR).fillna(1.0)
    goods["eur"] = goods.net_amount / fx
    goods["unit_price"] = goods.eur / goods.qty_kg.replace(0, np.nan)
    flagged = set()

    # 1. Duplicate postings: same supplier/material/quantity/amount/lot re-posted within 14 days.
    key = ["supplier_id", "material_id", "quantity", "net_amount"]
    rec = e[e.movement_type == 101].sort_values(["date", "doc_no"])
    for _, g in rec[rec.duplicated(key, keep=False)].groupby(key):
        g = g.sort_values(["date", "doc_no"])
        first = g.iloc[0]
        for _, d in g.iloc[1:].iterrows():
            same_lot = (pd.isna(first.lot_id) and pd.isna(d.lot_id)) or first.lot_id == d.lot_id
            if same_lot and (d.date - first.date).days <= 14:
                out.add("erp_purchase_lines", d.record_key, d.year, "duplicate_record", 0.95, "exclude", "*", None, None,
                        "ERP-DUP", f"Same supplier, material, quantity, amount and lot as {first.record_key} "
                        f"posted {(d.date - first.date).days} days earlier.", f"original={first.record_key}")
                flagged.add(d.record_key)

    # 2. Material mismatch: line material differs from the supplier master's material.
    for _, d in e[e.supplier_id.isin(sup.index)].iterrows():
        exp = sup.loc[d.supplier_id, "material_id"]
        if d.material_id != exp and d.record_key not in flagged:
            out.add("erp_purchase_lines", d.record_key, d.year, "material_mismatch", 0.9, "correct", "material_id",
                    d.material_id, exp, "ERP-MAT", f"Supplier {d.supplier_id} only supplies {exp} "
                    f"({mats.loc[exp, 'name_en']}) in the supplier master; line is coded {d.material_id}.",
                    "data/01_master/suppliers.csv")
            flagged.add(d.record_key)

    # 3. Currency: suppliers invoicing in SEK/USD whose line says EUR.
    modal = e.groupby("supplier_id").currency.agg(lambda x: x.value_counts().idxmax())
    for _, d in e[(e.currency == "EUR") & e.supplier_id.map(modal).isin(["SEK", "USD"])].iterrows():
        if d.record_key in flagged:
            continue
        cur = modal[d.supplier_id]
        out.add("erp_purchase_lines", d.record_key, d.year, "currency_error", 0.85, "correct", "currency", "EUR", cur,
                "ERP-CUR-SUPPLIER", f"Supplier {d.supplier_id} invoices in {cur} on "
                f"{(e[e.supplier_id == d.supplier_id].currency == cur).mean():.0%} of lines; this line is booked as EUR.")
        flagged.add(d.record_key)

    # 4. Spend lines (uom EUR) ~11.4x their peers: SEK amount booked as EUR.
    spend = e[(e.uom == "EUR") & (e.movement_type == 101)].copy()
    spend["med"] = spend.groupby("material_id").net_amount.transform("median")
    spend["ratio"] = spend.net_amount / spend.med
    for _, d in spend[(spend.ratio > 6)].iterrows():
        if d.record_key in flagged:
            continue
        new = round(d.net_amount / F.FX_PER_EUR["SEK"], 2)
        out.add("erp_purchase_lines", d.record_key, d.year, "currency_error", 0.8, "correct", "net_amount", d.net_amount, new,
                "ERP-CUR-SPEND", f"Spend line is {d.ratio:.1f}x the median for {d.material_id}; consistent with a SEK "
                f"amount booked as EUR (11.4 SEK/EUR).", f"median={d.med:.0f}")
        flagged.add(d.record_key)

    # 5. Unit price outliers on goods: tonnes keyed as KG (~1000x) and decimal shifts (~10x).
    med = goods.groupby(["material_id", "supplier_id"]).unit_price.transform("median")
    med = med.fillna(goods.groupby("material_id").unit_price.transform("median"))
    goods["ratio"] = goods.unit_price / med
    for _, d in goods.iterrows():
        if d.record_key in flagged or not np.isfinite(d.ratio):
            continue
        ratio = d.ratio
        if ratio > 300:
            out.add("erp_purchase_lines", d.record_key, d.year, "unit_error", 0.95, "correct", "quantity", d.quantity,
                    round(d.quantity * 1000, 3), "ERP-UNIT", f"Unit price {ratio:.0f}x the supplier median: tonnes keyed "
                    f"with uom KG.", f"unit_price={d.unit_price:.2f} EUR/kg")
        elif 6 < ratio < 16:
            out.add("erp_purchase_lines", d.record_key, d.year, "decimal_shift", 0.85, "correct", "quantity", d.quantity,
                    round(d.quantity * 10, 3), "ERP-DEC", f"Unit price {ratio:.1f}x the supplier median: quantity "
                    f"is one decimal place too small.")
        elif 1 / 16 < ratio < 1 / 6:
            out.add("erp_purchase_lines", d.record_key, d.year, "decimal_shift", 0.85, "correct", "quantity", d.quantity,
                    round(d.quantity / 10, 3), "ERP-DEC", f"Unit price {ratio:.2f}x the supplier median: quantity "
                    f"is one decimal place too large.")


# ---------------------------------------------------------------------------------------- energy
def _energy(r, out: _Collector):
    em = r["energy_meter"].copy()
    em["v_mwh"] = np.where(em.unit == "kWh", em.value / 1000, em.value)
    # unit errors first (kWh in MWh column): >200x the series median
    for (site, carrier, year), g in em.groupby(["site_id", "carrier", "year"]):
        medv = g.v_mwh.median()
        for _, d in g.iterrows():
            if medv > 0 and d.v_mwh > 200 * medv:
                out.add("energy_meter_readings", d.record_key, year, "unit_error", 0.95, "correct", "value", d.value,
                        round(d.value / 1000, 3), "EN-UNIT", f"Reading is {d.v_mwh / medv:.0f}x the {carrier} median for "
                        f"{site} {year}: kWh reported in the MWh column.")
                em.loc[d.name, "v_mwh"] = d.v_mwh / 1000
    # spikes: compare to the same month in other years (seasonality-aware) and to the series level
    em["level"] = em.groupby(["site_id", "carrier", "year"]).v_mwh.transform("median")
    em["norm"] = em.v_mwh / em.level
    for _, d in em.iterrows():
        peers = em[(em.site_id == d.site_id) & (em.carrier == d.carrier) & (em.month == d.month) & (em.year != d.year)]
        if len(peers) < 2 or d.level <= 0:
            continue
        pn = peers.norm.median()
        if pn > 0 and d.norm / pn > 2.5 and d.norm > 1.8:
            new = round(pn * d.level, 3)
            prior = [x for x in out.rows if x["record_key"] == d.record_key and x["issue_type"] == "unit_error"]
            if prior:   # unit slip *and* a spike on the same reading: chain both corrections
                prior[0]["new_value"] = str(new)
                prior[0]["explanation"] += f" After the unit fix the reading is still {d.norm / pn:.1f}x the seasonal level; replaced with the seasonal estimate."
                continue
            out.add("energy_meter_readings", d.record_key, d.year, "meter_spike", 0.8, "correct", "value", d.value, new,
                    "EN-SPIKE", f"{d.carrier} reading is {d.norm / pn:.1f}x the same month in other years (after "
                    f"seasonality); replaced with seasonal estimate.", f"peer_months={len(peers)}")
    # missing months inside a running series
    for (site, carrier, year), g in em.groupby(["site_id", "carrier", "year"]):
        months = set(g.month)
        prev_full = len(em[(em.site_id == site) & (em.carrier == carrier) & (em.year == year - 1)]) == 12
        nxt = em[(em.site_id == site) & (em.carrier == carrier) & (em.year == year + 1)]
        for m in range(1, 13):
            if m in months:
                continue
            inside = min(months) < m < max(months)
            tail = (m > max(months) and len(nxt) > 0) or (m < min(months) and prev_full)
            if len(months) >= 8 and (inside or tail):
                peers = em[(em.site_id == site) & (em.carrier == carrier) & (em.month == m) & (em.year != year)]
                est = peers.norm.median() * g.v_mwh.median() if len(peers) else g.v_mwh.median()
                out.add("energy_meter_readings", f"energy:{site}:{year}-{m:02d}:{carrier}", year, "missing_record", 0.85,
                        "add", "value", None, round(float(est), 3), "EN-GAP", f"Month {m:02d} missing from an otherwise "
                        f"continuous {carrier} series at {site}; gap-filled with the seasonal estimate.",
                        f"site={site};carrier={carrier};month={m}")


def _fuel(r, out: _Collector):
    fd = r["fuel_deliveries"]
    med = fd.groupby(["site_id", "carrier", "unit"]).quantity.transform("median")
    for _, d in fd[(fd.unit == "l") & (fd.quantity < med / 200)].iterrows():
        out.add("fuel_deliveries", d.record_key, d.year, "unit_error", 0.9, "correct", "quantity", d.quantity,
                round(d.quantity * 1000, 3), "FUEL-UNIT", f"{d.carrier} delivery of {d.quantity} l is "
                f"<1/200 of the typical delivery: m3 keyed as litres.")


def _goo(r, out: _Collector):
    reg = r["goo_registry"].copy()
    reg["prod_year"] = reg.production_period_start.str[:4].astype(int)
    for _, d in r["goo"].iterrows():
        rr = reg[(reg.beneficiary_site == d.site_id) & (reg.consumption_year == d.year)]
        if d.vintage_year != d.year and len(rr) and (rr.prod_year == d.year).all():
            out.add("goo_cancellations", d.record_key, d.year, "certificate_mismatch", 0.9, "correct", "vintage_year",
                    d.vintage_year, d.year, "GOO-REG", "Internal log vintage differs from the national registry "
                    "statement (authoritative); registry production periods fall in the consumption year.",
                    ";".join(rr.cancellation_id))


def _refrigerants(r, out: _Collector):
    rf = r["refrigerants"]
    modal = rf.groupby(["site_id", "system"]).refrigerant.agg(lambda x: x.value_counts().idxmax())
    med = rf.groupby(["site_id", "system"]).kg_added.transform("median")
    for _, d in rf.iterrows():
        sys_gas = modal[(d.site_id, d.system)]
        if d.refrigerant != sys_gas and (rf[(rf.site_id == d.site_id) & (rf.system == d.system)].refrigerant == sys_gas).mean() >= 0.6:
            out.add("refrigerant_service_log", d.record_key, d.year, "wrong_gas_code", 0.85, "correct", "refrigerant",
                    d.refrigerant, sys_gas, "REF-GAS", f"System '{d.system}' is serviced with {sys_gas} in every other "
                    f"event; this event is coded {d.refrigerant}.")
        if d.kg_added > 200 * max(med[d.name], 0.01):
            out.add("refrigerant_service_log", d.record_key, d.year, "unit_error", 0.9, "correct", "kg_added", d.kg_added,
                    round(d.kg_added / 1000, 3), "REF-UNIT", f"Top-up of {d.kg_added} kg is >200x the system median: "
                    f"grams in the kg column.")


def _waste(r, out: _Collector):
    w = r["waste"]
    med = w.groupby(["site_id", "waste_stream"]).quantity.transform("median")
    for _, d in w[w.quantity < med / 200].iterrows():
        out.add("waste_transfer_notes", d.record_key, d.year, "unit_error", 0.9, "correct", "quantity", d.quantity,
                round(d.quantity * 1000, 3), "WASTE-UNIT", f"{d.waste_stream} note is <1/200 of the stream median: "
                f"tonnes keyed as kg.")


def _travel(r, out: _Collector):
    t = r["travel"]
    dup = t[t.duplicated(["traveller_id", "travel_date", "route", "pkm", "nights"], keep="first")]
    for _, d in dup.iterrows():
        out.add("travel_bookings", d.record_key, d.year, "duplicate_record", 0.95, "exclude", "*", None, None,
                "TRAVEL-DUP", "Re-imported booking: same traveller, date, route, distance and nights as an earlier row.")


MODE_MAP = {"car": "car", "Car": "car", "auto": "car", "henkilöauto": "car", "linja-auto": "bus", "bussi": "bus",
            "juna": "train", "pyörä": "bike_walk", "kävely": "bike_walk"}


def _commuting(r, out: _Collector):
    c = r["commuting"]
    for _, d in c.iterrows():
        if d.one_way_km > 500:
            out.add("commuting_survey", d.record_key, 2025, "unit_error", 0.9, "correct", "one_way_km", d.one_way_km,
                    round(d.one_way_km / 1000, 2), "COM-UNIT", f"One-way distance {d.one_way_km} km is implausible: "
                    f"metres entered in the km field.")
        elif d["mode"] in MODE_MAP and d["mode"] != MODE_MAP[d["mode"]]:
            out.add("commuting_survey", d.record_key, 2025, "mode_mislabel", 0.7, "correct", "mode", d["mode"],
                    MODE_MAP[d["mode"]], "COM-MODE", f"Free-text mode '{d['mode']}' mapped to the survey code "
                    f"'{MODE_MAP[d['mode']]}'.")


def _shipments(r, out: _Collector):
    """Inbound lots whose material/origin normally has a sea leg but this document has none."""
    sh = r["shipments"]
    e = r["erp"]
    e2 = e[(e.movement_type == 101) & (e.uom != "EUR")][["doc_no", "material_id", "origin_country", "uom", "quantity", "posting_date"]].copy()
    e2["doc_no"] = e2.doc_no.astype(str)
    inb = sh[sh.direction == "inbound"].merge(e2, left_on="ref_doc", right_on="doc_no", how="left")
    legs = inb.groupby("ref_doc").ef_id.apply(set)
    info = inb.drop_duplicates("ref_doc").set_index("ref_doc")
    info["legs"] = legs
    sea = ["EF-TKM-SHIP-BULK", "EF-TKM-SHIP-REEFER", "EF-TKM-ROPAX"]
    for (mat, origin), g in info.groupby(["material_id", "origin_country"]):
        if len(g) < 4:
            continue
        for leg in sea:
            share = g.legs.apply(lambda s: leg in s).mean()
            if share < 0.75:
                continue
            km = inb[(inb.material_id == mat) & (inb.origin_country == origin) & (inb.ef_id == leg)].km.median()
            for ref, d in g[~g.legs.apply(lambda s: leg in s)].iterrows():
                t = inb[inb.ref_doc == ref].t.max()
                out.add("shipments", f"ship:{ref}:{leg}", int(d.year), "missing_leg", 0.8, "add", "tkm", None,
                        round(float(t * km), 2), "SHIP-LEG", f"{share:.0%} of {mat} lots from {origin} have a {leg} leg; "
                        f"document {ref} has only {sorted(d.legs)}. Leg added at the median distance {km:.0f} km.",
                        f"t={t:.3f};km={km:.1f}")


# ---------------------------------------------------------------------------------------- farming
def soil_share(r) -> pd.DataFrame:
    """Organic-soil share of each farm's crop area per year, from the soil map (≈88 % accurate)."""
    ch = r["crop_history"][r["crop_history"].status == "declared"]
    par = r["parcels"].set_index("parcel_id")[["area_ha", "soil_class_soilmap"]]
    x = ch.join(par, on="parcel_id")
    x["org_ha"] = np.where(x.soil_class_soilmap == "organic", x.area_ha, 0.0)
    g = x.groupby(["farm_id", "crop", "year"])[["area_ha", "org_ha"]].sum().reset_index()
    g["organic_share"] = g.org_ha / g.area_ha
    return g


def _farm_survey(r, out: _Collector):
    sv = r["farm_survey"]
    ss = soil_share(r)
    sv2 = sv.merge(ss, on=["farm_id", "crop", "year"], how="left", suffixes=("", "_parcels"))
    medy = sv.groupby(["crop"]).yield_t_ha.transform("median")
    for _, d in sv[sv.yield_t_ha > 3 * medy].iterrows():
        out.add("farm_survey", d.record_key, d.year, "outlier_implausible", 0.9, "correct", "yield_t_ha", d.yield_t_ha,
                round(d.yield_t_ha / 10, 3), "FS-YIELD", f"Yield {d.yield_t_ha} t/ha is >3x the {d.crop} median "
                f"({medy[d.name]:.2f}); decimal slip.")
    # decimal shifts in inputs vs the farm's own history for the crop
    for field in ["n_mineral_kg_ha", "p_kg_ha", "diesel_l_ha"]:
        fm = sv.groupby(["farm_id", "crop"])[field].transform("median")
        cm = sv.groupby(["crop", "year"])[field].transform("median")
        ref = fm.where(fm > 0, cm)
        for _, d in sv[(ref > 5) & (sv[field] > 0)].iterrows():
            ratio = d[field] / ref[d.name]
            if ratio < 0.14 or ratio > 7:
                new = d[field] * 10 if ratio < 1 else d[field] / 10
                out.add("farm_survey", d.record_key, d.year, "decimal_shift", 0.75, "correct", field, d[field],
                        round(new, 2), "FS-DEC", f"{field} = {d[field]} is {ratio:.2f}x the farm's usual value "
                        f"({ref[d.name]:.1f}); decimal slip.")
    # area vs declared parcels
    for _, d in sv2[sv2.area_ha_parcels.notna()].iterrows():
        dev = d.area_ha / d.area_ha_parcels - 1
        if abs(dev) > 0.25:
            out.add("farm_survey", d.record_key, d.year, "area_mismatch", 0.8, "correct", "area_ha", d.area_ha,
                    round(d.area_ha_parcels, 2), "FS-AREA", f"Reported {d.area_ha} ha differs {dev:+.0%} from the "
                    f"{d.area_ha_parcels:.2f} ha of declared {d.crop} parcels.", "parcel_crop_history_and_plan.csv")
    # soil misreport: ≥30 % organic per soil map while the survey says mineral
    for _, d in sv2[(sv2.organic_share >= 0.3) & sv2.soil_type_reported.str.lower().str.startswith("mineral")].iterrows():
        conf = 0.6 + 0.35 * min(1.0, (d.organic_share - 0.3) / 0.4)
        out.add("farm_survey", d.record_key, d.year, "soil_misreport", conf, "correct", "organic_share",
                0.0, round(d.organic_share, 3), "FS-SOIL", f"Soil map puts {d.organic_share:.0%} of this farm's "
                f"{d.year} {d.crop} area on organic soil, but the survey says 'Mineral soil'. The soil map is ~88 % "
                f"accurate; confirm with the grower.", "data/01_master/parcels.csv soil_class_soilmap")


def _farm_registry(r, out: _Collector):
    fr = r["farm_registry"]
    for _, d in fr[(fr.lat < fr.lon)].iterrows():
        out.add("farm_registry", d.record_key, C.REPORT_YEAR, "coordinate_swap", 0.95, "correct", "lat/lon",
                f"{d.lat},{d.lon}", f"{d.lon},{d.lat}", "FARM-COORD", "Latitude is smaller than longitude, which is "
                "impossible in the Nordic sourcing area: coordinates swapped.")


def _pcfs(r, out: _Collector):
    for _, d in r["pcfs"].iterrows():
        if d.value > 50:
            out.add("supplier_pcfs", d.record_key, d.year, "pcf_unit_error", 0.9, "correct", "pCfExcludingBiogenic",
                    d.value, round(d.value / 1000, 4), "PCF-UNIT", f"PCF {d.value} per 'kilogram' is ~1000x the "
                    f"supplier's other declarations: value is per tonne.", d.file)
        y0, y1 = int(d.ref_start[:4]), pd.Timestamp(d.ref_end)
        if not (pd.Timestamp(d.ref_start) <= pd.Timestamp(f"{d.year}-06-30") <= y1):
            out.add("supplier_pcfs", d.record_key, d.year, "pcf_stale_period", 0.9, "exclude", "referencePeriod",
                    f"{d.ref_start}..{d.ref_end[:10]}", None, "PCF-STALE", f"Reference period {d.ref_start[:4]} does not "
                    f"cover {d.year}; falls back to the material default factor (GHG Protocol data hierarchy).", d.file)


def _draft_report(r, out: _Collector):
    dr = r["draft_report"].set_index("line")
    for y, g in r["draft_report"].groupby("year"):
        g = g.set_index("line").tco2e
        parts = g.get("scope1", 0) + g.get("scope2_market", 0) + g.get("scope3", 0)
        if "total_market_based" in g and abs(parts - g["total_market_based"]) > 0.01 * max(parts, 1):
            out.add("draft_report_summary", f"report:{y}:total_market_based", y, "total_mismatch", 0.95, "flag", "tco2e",
                    g["total_market_based"], round(parts, 0), "REPORT-SUM", f"Total {g['total_market_based']:,.0f} does not "
                    f"equal Scope 1 + 2 (market) + 3 = {parts:,.0f}. Replace the draft with the reviewed inventory.")


RULES = [_erp, _energy, _fuel, _goo, _refrigerants, _waste, _travel, _commuting, _shipments, _farm_survey,
         _farm_registry, _pcfs, _draft_report]


def detect(r: dict | None = None) -> pd.DataFrame:
    r = r or raw()
    out = _Collector()
    for rule in RULES:
        rule(r, out)
    df = pd.DataFrame(out.rows)
    # one primary issue per record+field (keep the highest-confidence proposal)
    df = df.sort_values("confidence", ascending=False).drop_duplicates(["record_key", "field"]).sort_values(["table", "year", "record_key"])
    df.insert(0, "issue_id", [f"Q{i:05d}" for i in range(1, len(df) + 1)])
    return df[COLUMNS].reset_index(drop=True)


def a1_submission(issues: pd.DataFrame, year: int = C.REPORT_YEAR) -> pd.DataFrame:
    s = issues[issues.year == year].drop_duplicates("record_key")
    return s[["table", "record_key", "issue_type", "confidence", "explanation", "year"]]
