"""Gold layer: GHG inventory (GHG Protocol, AR6 GWP100) with record-level lineage and a FLAG split.

The calculation itself is the package's reference calculator (``ccn_tools.reference_calculator``),
which implements docs/METHODOLOGY.md. Juuri's contribution is what goes *into* it (reviewed,
corrected canonical tables) and what comes *out* of it: every line item is linked to the source
records, factors and corrections behind it.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from . import canonical as K
from . import config as C

C.use_reference_tools()
from ccn_tools import factors as F  # noqa: E402
from ccn_tools import reference_calculator as RC  # noqa: E402

CATEGORY_LABEL = {
    "stationary_and_mobile_combustion": "Fuel combustion", "fugitive_refrigerants": "Refrigerant leaks",
    "purchased_electricity": "Purchased electricity", "purchased_heat_steam": "Purchased heat & steam",
    "cat01_purchased_goods": "1 Purchased goods", "cat01_purchased_services": "1 Purchased services",
    "cat01_purchased_goods_services": "1 Purchased goods & services", "cat02_capital_goods": "2 Capital goods",
    "cat03_fuel_energy": "3 Fuel & energy related", "cat04_upstream_transport": "4 Upstream transport",
    "cat05_waste": "5 Waste", "cat06_business_travel": "6 Business travel", "cat07_employee_commuting": "7 Commuting",
    "cat09_downstream_transport": "9 Downstream transport", "cat11_use_of_sold_products": "11 Use of sold products",
    "cat12_end_of_life": "12 End of life",
}


@lru_cache(maxsize=4)
def _canonical(policy: str, decisions_mtime: float) -> dict:
    return K.build(policy)


def canonical(policy: str = "provisional") -> dict:
    """Silver tables; rebuilt automatically when a reviewer records a new decision."""
    mtime = C.DECISIONS_FILE.stat().st_mtime if C.DECISIONS_FILE.exists() else 0.0
    return _canonical(policy, mtime)


def line_items(T: dict, year: int) -> pd.DataFrame:
    inv = RC.compute_inventory(T, year)
    inv.attrs["biogenic_co2_t"] = inv.attrs.get("biogenic_co2_t", np.nan)
    return inv


def summary(T: dict, year: int) -> pd.DataFrame:
    return RC.summarise(line_items(T, year))


def flag_split(T: dict, year: int) -> pd.DataFrame:
    """FLAG vs non-FLAG per Scope 3 cat 1 line (factor flag_share; contract-farm FLAG components)."""
    inv = line_items(T, year)
    ef = T["ef_library"].set_index("ef_id")
    mats = T["materials"].set_index("material_id")
    pcf = T["pcf_used"]
    rows = []
    for _, r in inv.iterrows():
        flag = 0.0
        if r.category == "cat01_purchased_goods" and r.line_item.startswith("contract_farming"):
            _, region, crop = r.line_item.split(":")
            fd = T["farm_detail"]
            g = fd[(fd.year == year) & (fd.region == region) & (fd.crop == crop)]
            flag = g.flag_tco2e.sum() / max(g.tco2e.sum(), 1e-9) * r.tco2e
        elif r.category == "cat01_purchased_goods":
            mat = r.line_item.split(":")[0]
            share = ef.loc[mats.loc[mat, "default_ef_id"], "flag_share"]
            flag = r.tco2e * (share if pd.notna(share) else 0.0)
        rows.append(dict(scope=r.scope, category=r.category, line_item=r.line_item, tco2e=r.tco2e, flag_tco2e=flag))
    out = pd.DataFrame(rows)
    out["non_flag_tco2e"] = out.tco2e - out.flag_tco2e
    return out


def lineage(T: dict, year: int) -> pd.DataFrame:
    """Record-level contributions: which source rows (record keys) make up each line item."""
    EF = dict(zip(T["ef_library"].ef_id, T["ef_library"].kgco2e_per_unit))
    out = []

    def add(df, scope, category, line_item, activity, unit, ef, ef_source, tco2e):
        out.append(pd.DataFrame(dict(year=year, scope=scope, category=category, line_item=line_item,
                                     record_key=df.record_key.values, activity=activity, unit=unit, ef=ef,
                                     ef_source=ef_source, tco2e=tco2e)))

    en = T["energy"][T["energy"].year == year]
    fuels = en[~en.carrier.isin(["electricity", "steam", "district_heat"])]
    for (site, carrier), g in fuels.groupby(["site_id", "carrier"]):
        ef = F.FUEL_EF_T_PER_MWH[carrier]
        add(g, "scope1", "stationary_and_mobile_combustion", f"{site}:{carrier}", g.mwh.values, "MWh", ef,
            "Statistics Finland fuel classification (t/MWh)", g.mwh.values * ef)
    rf = T["refrigerants"][T["refrigerants"].year == year]
    for (site, gas), g in rf.groupby(["site_id", "refrigerant"]):
        gwp = F.REFRIGERANT_GWP[gas]
        add(g, "scope1", "fugitive_refrigerants", f"{site}:{gas}", g.kg_added.values, "kg", gwp, "IPCC AR6 GWP100",
            g.kg_added.values * gwp / 1000)
    country = T["sites"].set_index("site_id").country.to_dict()
    for site, g in en[en.carrier == "electricity"].groupby("site_id"):
        lb = (F.GRID_FI_LB if country.get(site) == "FI" else F.GRID_SE_LB)[year]
        add(g, "scope2_location", "purchased_electricity", site, g.mwh.values, "MWh", lb, "national grid average", g.mwh.values * lb)
    pur = T["purchases"][T["purchases"].year == year]
    mm = T["materials"].set_index("material_id")
    pcf = T["pcf_used"]
    pcf_y = pcf[pcf.year == year].set_index(["supplier_id", "material_id"]).kgco2e_per_kg.to_dict() if len(pcf) else {}
    goods = pur[~pur.material_id.map(mm.material_group).isin(["service", "capex"])]
    for (mat, sup), g in goods.groupby(["material_id", "supplier_id"]):
        if mm.loc[mat, "default_ef_id"] == "FARM":
            continue
        if (sup, mat) in pcf_y:
            ef, src = pcf_y[(sup, mat)], f"supplier PCF pcf:{sup}:{mat}:{year}"
        else:
            ef, src = EF[mm.loc[mat, "default_ef_id"]], mm.loc[mat, "default_ef_id"]
        add(g, "scope3", "cat01_purchased_goods", f"{mat}:{sup}", g.qty_kg.values, "kg", ef, src, g.qty_kg.values * ef / 1000)
    fd = T["farm_detail"]
    fd = fd[(fd.year == year) & (fd.region == "FI_SW")]
    out.append(pd.DataFrame(dict(year=year, scope="scope3", category="cat01_purchased_goods",
                                 line_item="contract_farming:FI_SW:" + fd.crop.values,
                                 record_key=("farm:" + fd.farm_id + ":" + fd.crop + " <- " + fd.records).values,
                                 activity=fd.area.values, unit="ha", ef=fd.kg_ha.values / 1000,
                                 ef_source=("farm footprint (" + fd.source + "; organic share " + fd.organic_share.round(2).astype(str) + ")").values,
                                 tco2e=fd.tco2e.values)))
    sh = T["shipments"][T["shipments"].year == year]
    for (direction, ef_id), g in sh.groupby(["direction", "ef_id"]):
        cat = "cat04_upstream_transport" if direction == "inbound" else "cat09_downstream_transport"
        add(g, "scope3", cat, f"{direction}:{ef_id}", g.tkm.values, "tkm", EF[ef_id], ef_id, g.tkm.values * EF[ef_id] / 1000)
    ws = T["waste"][T["waste"].year == year]
    for (site, stream, ef_id), g in ws.groupby(["site_id", "waste_stream", "ef_id"]):
        add(g, "scope3", "cat05_waste", f"{site}:{stream}", g.t.values, "t", EF[ef_id], ef_id, g.t.values * EF[ef_id] / 1000)
    return pd.concat(out, ignore_index=True)


def all_years(policy: str = "provisional") -> pd.DataFrame:
    T = canonical(policy)
    rows = []
    for y in C.INVENTORY_YEARS:
        s = summary(T, y)
        s["year"] = y
        rows.append(s)
    return pd.concat(rows, ignore_index=True)


def a2_submission(T: dict, year: int = C.REPORT_YEAR) -> pd.DataFrame:
    tpl = pd.read_csv(C.TASKS / "templates" / "A2_inventory_2025_submission.csv")
    s = summary(T, year).set_index(["scope", "category"]).tco2e
    tpl["tco2e"] = [round(float(s.get((a, b), np.nan)), 2) for a, b in zip(tpl.scope, tpl.category)]
    notes = {"cat01_purchased_goods_services": "PCF > default factor; FI contract farms: corrected survey + soil-map organic share",
             "scope2_market_total": "GoO from national registry (vintage = consumption year), residual mix",
             "cat04_upstream_transport": "GLEC WTW; missing sea legs imputed"}
    tpl["method_note"] = [notes.get(c, "juuri: reviewed corrections + reference calculator") for c in tpl.category]
    return tpl
