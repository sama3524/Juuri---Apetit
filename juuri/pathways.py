"""Track B: SBTi-aligned targets, base-year recalculation, scenario pathways and a MACC.

Principles (docs/SCENARIOS_AND_RISK.md):
* structural changes (acquisitions, divestments, closures) and measurement changes (lever L27) are
  shown separately and never counted as abatement;
* lever abatement is recomputed from the reviewed inventory where the data allows; otherwise the
  catalogue's design estimate is used and labelled as such;
* uncertainty is shown, scaled by the lever's confidence.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from . import inventory as INV
from .data import raw

C.use_reference_tools()
from ccn_tools import factors as F  # noqa: E402
from ccn_tools.farm_calculator import farm_footprint_per_ha  # noqa: E402

RATES = {"scope12": 0.042, "scope3_nonflag": 0.025, "flag": 0.0303}
RATE_SOURCE = {"scope12": "SBTi 1.5 °C, 4.2 %/yr linear (V1.3.1 / V2)",
               "scope3_nonflag": "SBTi well-below 2 °C, 2.5 %/yr linear",
               "flag": "SBTi FLAG guidance, 3.03 %/yr linear"}
UNCERTAINTY = {"high": 0.2, "medium": 0.4, "low": 0.6}
DISCOUNT = 0.08


def _years(a, b):
    return list(range(a, b + 1))


# ------------------------------------------------------------------------------ Foodhills pro forma
def foodhills_farms(year: int = 2025) -> pd.DataFrame:
    """Swedish (Foodhills) pea growers' cradle-to-gate emissions, pro forma for a full year in scope."""
    r = raw()
    fh = r["foodhills"].rename(columns={"odlar_id": "farm_id", "skördeår": "year", "areal_ha": "area_ha",
                                        "levererad_mängd_ton": "delivered_t", "kvävegiva_kg_ha": "n_mineral_kg_ha"})
    fh = fh[fh.year == year].copy()
    norm = F.MGMT_NORMS["pea"]
    fh["crop"] = "pea"
    fh["yield_t_ha"] = fh.delivered_t / fh.area_ha
    fh["n_manure_kg_ha"] = 0.0
    fh["p_kg_ha"] = norm["p"]
    fh["lime_t_ha"] = norm["lime"] / 4      # liming every ~4 years
    fh["sprays"] = norm["sprays"]
    fh["irrigated"] = fh.bevattnad.str.lower().eq("ja")
    par = r["parcels"]
    org = par[par.farm_id.str.startswith("S")].assign(o=lambda d: (d.soil_class_soilmap == "organic") * d.area_ha)
    share = org.groupby("farm_id").o.sum() / org.groupby("farm_id").area_ha.sum()
    fh["organic_share"] = fh.farm_id.map(share).fillna(0.0)
    m = farm_footprint_per_ha(fh.assign(organic_soil=False))
    o = farm_footprint_per_ha(fh.assign(organic_soil=True))
    fh["kg_ha"] = m + fh.organic_share * (o - m) + fh.diesel_l_ha * (2.45 - F.FARM_EF["diesel_ttw"])  # Swedish diesel blend
    fh["tco2e"] = fh.kg_ha * fh.area_ha / 1000
    E = F.FARM_EF
    n2o = E["n2o_n_to_n2o"] * E["n2o_gwp"]
    res_n = F.RESIDUE_N_PER_T["pea"] * fh.yield_t_ha
    flag_ha = ((fh.n_mineral_kg_ha * E["ef1_synthetic"] + res_n * E["ef1_organic"]) * n2o
               + (fh.n_mineral_kg_ha * E["frac_gasf"] * E["ef4_n2o_n"] + (fh.n_mineral_kg_ha + res_n) * E["frac_leach"] * E["ef5_n2o_n"]) * n2o
               + fh.lime_t_ha * E["lime"] * 1000 + fh.organic_share * (o - m))
    fh["flag_tco2e"] = flag_ha * fh.area_ha / 1000
    fh["fert_tco2e"] = fh.n_mineral_kg_ha * E["fert_n_prod"] * fh.area_ha / 1000
    return fh


# ------------------------------------------------------------------------------ base years
def base_inventory(policy: str = "provisional") -> dict:
    """2025 inventory split into target boundaries, with the Foodhills structural adjustment."""
    T = INV.canonical(policy)
    inv = INV.line_items(T, 2025)
    fl = INV.flag_split(T, 2025)
    s1 = inv[inv.scope == "scope1"].tco2e.sum()
    s2m = inv[inv.scope == "scope2_market"].tco2e.sum()
    s3 = inv[inv.scope == "scope3"].tco2e.sum()
    flag = fl.flag_tco2e.sum()
    # Foodhills: Bjuv operations are in scope for December only -> annualise; SE growers enter scope 3.
    bju_s1 = inv[(inv.scope == "scope1") & inv.line_item.str.startswith("S-BJU")].tco2e.sum()
    bju_s2 = inv[(inv.scope == "scope2_market") & inv.line_item.str.startswith("S-BJU")].tco2e.sum()
    se = foodhills_farms(2025)
    adj = dict(scope1=bju_s1 * 11, scope2=bju_s2 * 11, scope3_flag=se.flag_tco2e.sum(),
               scope3_nonflag=se.tco2e.sum() - se.flag_tco2e.sum())
    reported = dict(scope1=s1, scope2=s2m, scope3_flag=flag, scope3_nonflag=s3 - flag)
    adjusted = {k: reported[k] + adj[k] for k in reported}
    return dict(reported=reported, adjustment=adj, adjusted=adjusted, line_items=inv, flag=fl, T=T, se_farms=se)


def base_year_options(b: dict) -> pd.DataFrame:
    """Compare a recalculated 2019 base with a 2025 base (SBTi V2 prefers the latest complete year)."""
    pub = raw()["published"].set_index("year")
    grain_2022 = pub.loc[2022, "scope3"] - pub.loc[2022, "scope3_restated"]   # 5 months of grain trade
    grain_annual = grain_2022 * 12 / 5
    a = b["adjustment"]
    rows = [
        dict(option="2019 published", scope1=pub.loc[2019, "scope1"], scope2=pub.loc[2019, "scope2_market"],
             scope3=pub.loc[2019, "scope3"], note="Includes divested grain trade; excludes Foodhills. Not a valid base as published."),
        dict(option="2019 recalculated", scope1=pub.loc[2019, "scope1"] + a["scope1"], scope2=pub.loc[2019, "scope2_market"] + a["scope2"],
             scope3=pub.loc[2019, "scope3"] - grain_annual + a["scope3_flag"] + a["scope3_nonflag"],
             note=f"− grain trade ≈{grain_annual:,.0f} t (annualised from the 2022 restatement); + Foodhills pro forma. "
                  "Method differs from the 2025 calculation (e.g. organic soils, PCFs): recalculate 2019 with the 2025 method before use."),
        dict(option="2025 reported", scope1=b["reported"]["scope1"], scope2=b["reported"]["scope2"],
             scope3=b["reported"]["scope3_flag"] + b["reported"]["scope3_nonflag"], note="Foodhills operations Dec only; SE growers not in scope."),
        dict(option="2025 structurally adjusted (recommended)", scope1=b["adjusted"]["scope1"], scope2=b["adjusted"]["scope2"],
             scope3=b["adjusted"]["scope3_flag"] + b["adjusted"]["scope3_nonflag"],
             note="Full-year Bjuv and ~300 SE pea growers added: the >5 % significance threshold is triggered, so recalculation is required."),
    ]
    df = pd.DataFrame(rows)
    df["scope12"] = df.scope1 + df.scope2
    return df


def targets(b: dict, base_year: int = 2025, target_year: int = 2032) -> pd.DataFrame:
    a = b["adjusted"]
    base = {"scope12": a["scope1"] + a["scope2"], "scope3_nonflag": a["scope3_nonflag"], "flag": a["scope3_flag"]}
    n = target_year - base_year
    rows = []
    for k, v in base.items():
        rows.append(dict(boundary=k, base_year=base_year, base_tco2e=v, rate_per_yr=RATES[k], target_year=target_year,
                         reduction_pct=100 * RATES[k] * n, target_tco2e=v * (1 - RATES[k] * n), source=RATE_SOURCE[k]))
    return pd.DataFrame(rows)


def scope3_coverage(b: dict) -> pd.DataFrame:
    s = INV.RC.summarise(b["line_items"])
    s = s[s.scope == "scope3"].copy()
    s["share_of_scope3"] = s.tco2e / s.tco2e.sum()
    s["must_be_covered_v2"] = s.share_of_scope3 >= 0.05
    return s.sort_values("tco2e", ascending=False)


# ------------------------------------------------------------------------------ levers
def lever_table(b: dict) -> pd.DataFrame:
    """Lever catalogue with abatement recomputed from the reviewed 2025 inventory where possible."""
    lv = raw()["levers"].copy()
    inv = b["line_items"]
    T = b["T"]
    fd = T["farm_detail"]
    fd = fd[(fd.year == 2025) & (fd.region == "FI_SW")]
    se = b["se_farms"]

    def li(scope, prefix=None, cat=None, contains=None):
        x = inv[inv.scope == scope]
        if cat:
            x = x[x.category == cat]
        if prefix:
            x = x[x.line_item.str.startswith(prefix)]
        if contains:
            x = x[x.line_item.str.contains(contains)]
        return x.tco2e.sum()

    hfc = inv[(inv.category == "fugitive_refrigerants") & inv.line_item.str.contains("R404A|R507A|R449A|R134a|R410A")].tco2e.sum()
    r404 = inv[(inv.category == "fugitive_refrigerants") & inv.line_item.str.contains("R404A|R507A")].tco2e.sum()
    E = F.FARM_EF
    fert_fi = (fd.n_mineral_kg_ha * fd.area * E["fert_n_prod"] / 1000).sum()
    lowc_share = fd.fertiliser_product.str.contains("low-carbon").mean()
    fert = (fert_fi * (1 - lowc_share) + se.fert_tco2e.sum()) * (1 - E["fert_n_prod_lowcarbon"] / E["fert_n_prod"])
    rapeseed_import = inv[(inv.category == "cat01_purchased_goods") & inv.line_item.str.match(r"M10[1-7]:")].tco2e.sum()
    trucks = inv[inv.line_item.str.contains("EF-TKM-TRUCK")].tco2e.sum()
    ng_bju = li("scope1", "S-BJU:natural_gas") * 12
    recompute = {
        "L01": (li("scope1", "S-SAK:lng") + li("scope1", "S-SAK:lpg"), "Säkylä LNG + LPG combustion 2025"),
        "L03": (0.8 * r404, "80 % of R404A/R507A top-up emissions 2025"),
        "L04": (0.3 * max(hfc - 0.8 * r404, 0), "30 % of remaining HFC leaks after L03"),
        "L07": (ng_bju, "Bjuv natural gas, Dec 2025 x 12"),
        "L08": (0.8 * fert, "80 % adoption of low-carbon AN (0.9 vs 3.7 kg CO2e/kg N), FI + SE growers"),
        "L11": (0.5 * fd.organic_tco2e.sum(), "Move half of contract area on drained organic soils to mineral soils"),
        "L13": (0.1 * rapeseed_import, "10 % lower PCF on traded/imported rapeseed via supplier programmes (catalogue design ≈ 14 kt)"),
        "L20": (0.85 * trucks, "HVO100 cuts ~85 % WTW on all truck legs (cat 4 + 9)"),
    }
    lv["abatement_tco2e"] = lv.abatement_tco2e_2030_design.astype(float)
    lv["abatement_basis"] = "design estimate (catalogue)"
    for k, (v, why) in recompute.items():
        if v > 0:
            lv.loc[lv.lever_id == k, "abatement_tco2e"] = round(v, 0)
            lv.loc[lv.lever_id == k, "abatement_basis"] = f"recomputed from 2025 inventory: {why}"
    crf = DISCOUNT / (1 - (1 + DISCOUNT) ** -lv.lifetime_yr.clip(lower=1))
    lv["annualised_cost_eur"] = lv.capex_eur * crf + lv.opex_delta_eur_per_yr
    lv["cost_eur_per_t"] = np.where(lv.abatement_tco2e > 0, lv.annualised_cost_eur / lv.abatement_tco2e.replace(0, np.nan), np.nan)
    lv["kind"] = np.where(lv.lever_type.isin(["structural"]), "structural (not abatement)",
                          np.where(lv.lever_type.eq("data"), "measurement (not abatement)", "abatement"))
    lv["uncertainty_frac"] = lv.confidence.map(UNCERTAINTY).fillna(0.5)
    lv["boundary"] = np.where(lv.scope.str.startswith("scope1") | lv.scope.str.startswith("scope2"), "scope12",
                              np.where(lv.flag, "flag", "scope3_nonflag"))
    lv.loc[lv.lever_id == "L11", "boundary"] = "flag"
    owners = {"energy": "Plant managers + energy partner", "refrigerants": "Plant maintenance", "farm_inputs": "Farming services (field advisors)",
              "farm_practice": "Farming services (field advisors)", "land_use": "Farming services + sourcing", "supplier_engagement": "Procurement",
              "sourcing": "Procurement", "product": "R&D / product development", "packaging": "Packaging development",
              "logistics": "Logistics", "cold_chain": "Supply chain + sales", "efficiency": "Production + farming services",
              "travel": "HR", "commuting": "HR + Säkylä site", "waste": "Plant managers", "data": "Sustainability team", "structural": "Management"}
    lv["owner"] = lv.lever_type.map(owners).fillna("Sustainability team")
    return lv


def scenario_prices(scenario: str) -> pd.DataFrame:
    s = raw()["scenarios"]
    s = s[s.scenario == scenario].set_index("year")
    idx = pd.Index(_years(2025, 2050), name="year")
    return s.reindex(idx).interpolate().ffill()[["carbon_price_eur_t", "eu_ets2_fuel_price_eur_t", "electricity_price_eur_mwh",
                                                  "n_fertiliser_price_index", "diesel_price_index", "plant_based_demand_index",
                                                  "frozen_veg_import_premium_pct"]]


def pathway(b: dict, scenario: str = "net_zero_2050", levers: list[str] | None = None, end: int = 2035,
            target_year: int = 2032) -> pd.DataFrame:
    """Yearly BAU and with-levers emissions per target boundary under a transition scenario."""
    lv = lever_table(b)
    lv = lv[lv.kind == "abatement"]
    if levers is not None:
        lv = lv[lv.lever_id.isin(levers)]
    a = b["adjusted"]
    inv = b["line_items"]
    pud = inv[inv.line_item.str.startswith("S-PUD") & inv.scope.isin(["scope1", "scope2_market"])].tco2e.sum()
    px = scenario_prices(scenario)
    rows = []
    for y in _years(2025, end):
        vol = px.loc[y, "plant_based_demand_index"]
        s12 = (a["scope1"] + a["scope2"]) * vol
        s3n = a["scope3_nonflag"] * vol
        fl = a["scope3_flag"] * vol
        structural = 0.0
        if y >= 2027:   # Pudasjärvi closed; pizza contract-manufactured -> emissions move to cat 1
            s12 -= pud * vol
            s3n += pud * vol
            structural = pud * vol
        row = dict(year=y, scenario=scenario, bau_scope12=s12, bau_scope3_nonflag=s3n, bau_flag=fl, structural_shift=structural)
        ab = {"scope12": 0.0, "scope3_nonflag": 0.0, "flag": 0.0}
        cost = 0.0
        for _, l in lv.iterrows():
            if y < l.earliest_start:
                continue
            ramp = min(1.0, (y - l.earliest_start + 1) / max(l.ramp_years, 1))
            t = l.abatement_tco2e * ramp * vol
            ab[l.boundary] += t
            cost += l.annualised_cost_eur * ramp
            if l.boundary == "scope12" or l.lever_id == "L20":
                cost -= t * px.loc[y, "eu_ets2_fuel_price_eur_t"]   # avoided ETS2 fuel cost
        row.update({f"abatement_{k}": v for k, v in ab.items()})
        row.update(dict(net_scope12=s12 - ab["scope12"], net_scope3_nonflag=s3n - ab["scope3_nonflag"], net_flag=fl - ab["flag"],
                        lever_cost_eur=cost))
        rows.append(row)
    df = pd.DataFrame(rows)
    base = {"scope12": a["scope1"] + a["scope2"], "scope3_nonflag": a["scope3_nonflag"], "flag": a["scope3_flag"]}
    for k, v in base.items():
        df[f"target_{k}"] = v * (1 - RATES[k] * (df.year - 2025)).clip(lower=0)
        df[f"gap_{k}"] = df[f"net_{k}"] - df[f"target_{k}"]
    return df


def macc(b: dict, scenario: str = "net_zero_2050", year: int = 2030) -> pd.DataFrame:
    lv = lever_table(b)
    lv = lv[(lv.kind == "abatement") & (lv.abatement_tco2e > 0)].copy()
    px = scenario_prices(scenario)
    ets2 = px.loc[year, "eu_ets2_fuel_price_eur_t"]
    fuel = (lv.boundary == "scope12") | (lv.lever_id == "L20")
    lv["cost_eur_per_t_scenario"] = lv.cost_eur_per_t - np.where(fuel, ets2, 0.0)
    lv["low_t"] = lv.abatement_tco2e * (1 - lv.uncertainty_frac)
    lv["high_t"] = lv.abatement_tco2e * (1 + lv.uncertainty_frac)
    lv = lv.sort_values("cost_eur_per_t_scenario")
    lv["x_end"] = lv.abatement_tco2e.cumsum()
    lv["x_start"] = lv.x_end - lv.abatement_tco2e
    return lv
