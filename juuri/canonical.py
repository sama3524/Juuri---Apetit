"""Silver layer: bronze (raw) + applied corrections -> canonical clean tables (METHODOLOGY.md §2).

Every applied correction is written to the audit log with the rule, evidence, proposer, reviewer
and status, so gold numbers are reproducible from bronze plus the log.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import config as C
from . import quality as Q
from . import review as RV
from .data import raw

C.use_reference_tools()
from ccn_tools import factors as F  # noqa: E402
from ccn_tools.farm_calculator import farm_footprint_per_ha  # noqa: E402

TABLE_OF = {"erp_purchase_lines": "erp", "energy_meter_readings": "energy_meter", "fuel_deliveries": "fuel_deliveries",
            "goo_cancellations": "goo", "refrigerant_service_log": "refrigerants", "waste_transfer_notes": "waste",
            "travel_bookings": "travel", "commuting_survey": "commuting", "farm_survey": "farm_survey",
            "farm_registry": "farm_registry", "supplier_pcfs": "pcfs"}
FIELD_OF = {"pCfExcludingBiogenic": "value"}
IMPUTE_OWN_HISTORY = True


def _cast(v, like):
    if isinstance(like, (bool, np.bool_)):
        return str(v).lower() == "true"
    if isinstance(like, (int, float, np.integer, np.floating)) or like is None or (isinstance(like, float) and np.isnan(like)):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    return v


def apply_corrections(r: dict, resolved: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Return corrected copies of the raw tables and the audit log of applied corrections."""
    t = {k: (v.copy() if isinstance(v, pd.DataFrame) else v) for k, v in r.items()}
    t["farm_survey"]["organic_share"] = 0.0
    t["farm_survey"].loc[t["farm_survey"].soil_type_reported.str.lower().str.startswith("organic"), "organic_share"] = 1.0
    added_energy, added_legs, log = [], [], []
    now = dt.datetime.now().isoformat(timespec="seconds")
    for _, i in resolved[resolved.applied].iterrows():
        name = TABLE_OF.get(i.table)
        if i.action == "add":
            if i.table == "energy_meter_readings":
                _, site, ym, carrier = i.record_key.split(":")
                added_energy.append(dict(site_id=site, year=int(ym[:4]), month=int(ym[5:]), carrier=carrier,
                                         value=float(i.new_value), unit="MWh", record_key=i.record_key))
            elif i.table == "shipments":
                _, ref, ef = i.record_key.split(":")
                added_legs.append(dict(ref_doc=ref, ef_id=ef, tkm=float(i.new_value), year=int(i.year)))
        elif name is not None:
            df = t[name]
            idx = df.index[df.record_key == i.record_key]
            if not len(idx):
                continue
            if i.action == "exclude":
                t[name] = df.drop(idx)
            elif i.field == "lat/lon":
                lat, lon = df.loc[idx, "lat"].values, df.loc[idx, "lon"].values
                df.loc[idx, "lat"], df.loc[idx, "lon"] = lon, lat
            else:
                col = FIELD_OF.get(i.field, i.field)
                df.loc[idx, col] = _cast(i.new_value, df.loc[idx[0], col] if col in df else None)
        log.append(dict(record_key=i.record_key, table=i.table, field=i.field, action=i.action, old_value=i.old_value,
                        new_value=i.new_value, issue_type=i.issue_type, rule_or_model=i.rule, evidence=i.evidence,
                        explanation=i.explanation, confidence=i.confidence, proposed_by="juuri-dq-agent",
                        approved_by=i.reviewer if i.status == "approved" else "", status=i.applied_as,
                        timestamp=i.timestamp if isinstance(i.timestamp, str) else now))
    if added_energy:
        t["energy_meter"] = pd.concat([t["energy_meter"], pd.DataFrame(added_energy)], ignore_index=True)
    if added_legs:
        sh = t["shipments"]
        add = pd.DataFrame(added_legs)
        add["direction"] = "inbound"
        add["mode"] = add.ef_id.str.replace("EF-TKM-", "").str.lower()
        add["shipment_id"] = "ADDED"
        add["record_key"] = "ship:" + add.ref_doc + ":" + add.ef_id
        t["shipments"] = pd.concat([sh, add], ignore_index=True)
    return t, pd.DataFrame(log)


# ------------------------------------------------------------------------------ farm footprints
def farm_footprints(t: dict, purchases: pd.DataFrame) -> pd.DataFrame:
    """Contract-farm cradle-to-gate emissions per farm x crop x year (Finnish growers).

    * respondents: corrected survey inputs, organic share from survey or soil map (whichever applied);
    * non-respondents: crop-year median inputs of respondents, organic share from the soil map;
    * total = per-ha footprint x crop area (emissions stay with delivered product: METHODOLOGY §5).
    """
    sv = t["farm_survey"].copy()
    ss = Q.soil_share(t)
    deliveries = purchases[purchases.material_id.isin(["M100", "M110", "M111", "M112", "M113"])].copy()
    deliveries["crop"] = deliveries.material_id.map(C.CROP_MATERIAL)
    d = deliveries.groupby(["supplier_id", "crop", "year"]).agg(delivered_kg=("qty_kg", "sum"),
                                                              records=("record_key", lambda x: ";".join(x))).reset_index()
    d = d.rename(columns={"supplier_id": "farm_id"})
    d = d.merge(sv, on=["farm_id", "crop", "year"], how="left")
    d["source"] = np.where(d.survey_id.notna(), "survey", "imputed (crop-year median of respondents)")
    num = ["n_mineral_kg_ha", "n_manure_kg_ha", "p_kg_ha", "diesel_l_ha", "lime_t_ha", "sprays", "yield_t_ha", "catch_crop_share"]
    med = sv.groupby(["crop", "year"])[num].median()
    own = sv.groupby(["farm_id", "crop"])[num].median()
    for c in num:
        f_own = pd.Series([own[c].get((f, cr), np.nan) for f, cr in zip(d.farm_id, d.crop)], index=d.index)
        f_med = pd.Series([med[c].get((cr, y), np.nan) for cr, y in zip(d.crop, d.year)], index=d.index)
        d[c] = d[c].fillna(f_own if IMPUTE_OWN_HISTORY else f_med).fillna(f_med)
    d["irrigated"] = d.irrigated.astype("boolean").fillna(False).astype(bool)
    d["fertiliser_product"] = d.fertiliser_product.fillna("")
    d = d.merge(ss[["farm_id", "crop", "year", "area_ha", "organic_share"]].rename(
        columns={"area_ha": "parcel_area_ha", "organic_share": "soilmap_share"}), on=["farm_id", "crop", "year"], how="left")
    # Expected organic share: the soil map is the best evidence unless the grower reports organic soil
    # (current practice assumes mineral soil everywhere, which understates FLAG emissions).
    d["organic_share"] = np.maximum(d.organic_share.fillna(0.0), d.soilmap_share.fillna(0.0))
    d["area"] = d.area_ha.fillna(d.parcel_area_ha)
    d["area"] = d.area.fillna(d.delivered_kg / (d.yield_t_ha * 1000))
    mineral = farm_footprint_per_ha(d.assign(organic_soil=False))
    organic = farm_footprint_per_ha(d.assign(organic_soil=True))
    d["kg_ha_mineral"] = mineral
    d["kg_ha_organic_extra"] = organic - mineral
    d["kg_ha"] = mineral + d.organic_share * (organic - mineral)
    d["tco2e"] = d.kg_ha * d.area / 1000
    # FLAG part: N2O (direct+indirect), liming, organic soils
    E = F.FARM_EF
    n2o = E["n2o_n_to_n2o"] * E["n2o_gwp"]
    res_n = d.crop.map(F.RESIDUE_N_PER_T) * d.yield_t_ha
    nmin, nman = d.n_mineral_kg_ha.fillna(0), d.n_manure_kg_ha.fillna(0)
    flag_ha = ((nmin * E["ef1_synthetic"] + (nman + res_n) * E["ef1_organic"]) * n2o
               + ((nmin * E["frac_gasf"] + nman * E["frac_gasm"]) * E["ef4_n2o_n"] + (nmin + nman + res_n) * E["frac_leach"] * E["ef5_n2o_n"]) * n2o
               + d.lime_t_ha.fillna(0) * E["lime"] * 1000 + d.organic_share * d.kg_ha_organic_extra)
    d["flag_tco2e"] = flag_ha * d.area / 1000
    d["organic_tco2e"] = d.organic_share * d.kg_ha_organic_extra * d.area / 1000
    d["region"] = np.where(d.farm_id.str.startswith("S"), "SE_SKANE", "FI_SW")
    return d


# ------------------------------------------------------------------------------ canonical tables
def build(policy: str = "provisional", issues: pd.DataFrame | None = None) -> dict:
    r = raw()
    issues = Q.detect(r) if issues is None else issues
    resolved = RV.resolve(issues, policy)
    t, audit = apply_corrections(r, resolved)
    T = {k: t[k] for k in ("sites", "materials", "ef_library", "heat_suppliers", "headcount", "warehousing", "wastewater")}
    erp = t["erp"].copy()
    erp["year"] = erp.posting_date.str[:4].astype(int)
    erp["qty_kg"] = np.where(erp.uom == "TO", erp.quantity * 1000, np.where(erp.uom == "KG", erp.quantity, 0.0))
    erp["amount_eur"] = erp.net_amount / erp.currency.map(F.FX_PER_EUR).fillna(1.0)
    T["purchases"] = erp
    em = t["energy_meter"].copy()
    em["mwh"] = np.where(em.unit == "kWh", em.value / 1000, em.value)
    fd = t["fuel_deliveries"]
    ncv = {"lng": F.NCV["lng_t"], "lpg": F.NCV["lpg_t"], "light_fuel_oil": F.NCV["lfo_l"], "diesel": F.NCV["diesel_l"]}
    fdy = fd.groupby(["site_id", "carrier", "year"]).agg(quantity=("quantity", "sum"), records=("record_key", lambda x: ";".join(x))).reset_index()
    fdy = fdy.merge(t["fuel_stock"][["site_id", "carrier", "year", "stock_change"]], on=["site_id", "carrier", "year"], how="left").fillna({"stock_change": 0})
    fdy["mwh"] = (fdy.quantity - fdy.stock_change) * fdy.carrier.map(ncv)
    fdy["month"] = 0
    fdy["record_key"] = "fuel_year:" + fdy.site_id + ":" + fdy.carrier + ":" + fdy.year.astype(str)
    T["energy"] = pd.concat([em[["site_id", "year", "month", "carrier", "mwh", "record_key"]],
                             fdy[["site_id", "year", "month", "carrier", "mwh", "record_key"]]], ignore_index=True)
    T["fuel_years"] = fdy
    T["refrigerants"] = t["refrigerants"][["site_id", "year", "refrigerant", "kg_added", "record_key", "system"]]
    # GoO: the national registry is authoritative; a cancellation counts only for its production year.
    reg = t["goo_registry"].copy()
    reg["vintage_year"] = reg.production_period_start.str[:4].astype(int)
    T["goo"] = reg.rename(columns={"consumption_year": "year", "beneficiary_site": "site_id", "mwh": "mwh_cancelled"})
    w = t["waste"].copy()
    w["t"] = w.quantity / 1000
    efmap = {"anaerobic_digestion": "EF-WASTE-BIO-AD", "composting": "EF-WASTE-BIO-COMPOST", "incineration": "EF-WASTE-MIXED-INC",
             "recycling": "EF-WASTE-RECYCLE", "landfill_codigestion": "EF-WASTE-SPENTEARTH"}
    w["ef_id"] = w.treatment.map(efmap)
    w.loc[w.waste_stream == "plastic_waste", "ef_id"] = "EF-WASTE-PLASTIC-INC"
    T["waste"] = w
    T["travel"] = t["travel"]
    T["commuting_survey"] = t["commuting"]
    T["shipments"] = t["shipments"]
    T["sales"] = t["sales"]
    p = t["pcfs"]
    T["pcf_used"] = p.rename(columns={"value": "kgco2e_per_kg"})[["year", "supplier_id", "material_id", "kgco2e_per_kg", "record_key", "file"]]
    ff = farm_footprints(t, erp)
    T["farm_detail"] = ff
    T["farm_footprints"] = ff[ff.region == "FI_SW"][["farm_id", "crop", "year", "region", "tco2e"]]
    T["_issues"] = resolved
    T["_audit"] = audit
    T["_tables"] = t
    return T
