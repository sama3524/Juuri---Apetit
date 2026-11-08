"""Track D: nature (SBTN-style).

D1 - field-to-water nutrient model per parcel (export coefficients modulated by soil-P, slope,
clay, crop erosion risk, rainfall, buffer strips, catch crops, drainage, N surplus), delivery that
decays with distance to water, then catchment retention. Two free parameters per nutrient (a scale
and a delivery length) are calibrated on the six public dev catchments; the monitored outlet loads
are used as a plausibility bound (Apetit's share must stay well below the total load).

D2 - value-chain materiality screening: 2025 volumes and emissions by material and origin x
indicative state of nature x ENCORE-style pressures, with SBTN high-impact and EUDR flags.

Geometry is read from the GeoJSON layers, so the same code runs on real SYKE / Ruokavirasto layers.
"""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd

from . import config as C
from .data import raw

STATE_WEIGHT = {"good": 1.0, "moderate": 1.2, "poor": 1.5, "bad": 1.8}
EROSION = {"potato": 1.6, "carrot": 1.5, "spinach": 1.25, "pea": 1.0, "turnip_rape": 0.9}
N_UPTAKE_PER_T = {"pea": 0.0, "potato": 3.5, "carrot": 1.5, "spinach": 4.0, "turnip_rape": 35.0}
P_EXPORT_KG_HA = 1.1     # SYKE Finnish average field P loss (SIMULATION_DESIGN.md)
N_EXPORT_KG_HA = 15.0    # SYKE Finnish average field N loss


@lru_cache(maxsize=1)
def parcel_risk(year: int = 2025) -> pd.DataFrame:
    """Per-parcel relative P and N loss factors for Apetit-crop parcels in `year`."""
    r = raw()
    ch = r["crop_history"]
    ch = ch[(ch.year == year) & ch.crop.isin(C.APETIT_CROPS)]
    p = r["parcels"].set_index("parcel_id")
    x = ch.join(p.drop(columns=["farm_id"]), on="parcel_id")
    from .climate import parcels as cparcels, weather_features
    x["cell_id"] = x.parcel_id.map(cparcels().set_index("parcel_id").cell_id)
    wf = weather_features()
    wf = wf[(wf.scenario == "historical") & (wf.year == year)].set_index("cell_id")
    rain = x.cell_id.map(wf.precip_season)
    rain_f = (rain / rain.median()).fillna(1.0)
    ptest = x.p_test_mgl_latest.fillna(x.groupby("farm_id").p_test_mgl_latest.transform("median")).fillna(x.p_test_mgl_latest.median())
    x["p_test_filled"] = ptest
    x["f_p"] = ((ptest / ptest.median()) ** 0.6 * (1 + 0.15 * (x.slope_pct - 2)).clip(0.5, 2.5)
                * (1 + 0.012 * (x.clay_pct_soilmap - 25)).clip(0.6, 1.6) * x.crop.map(EROSION)
                * np.where(x.buffer_strip, 0.7, 1.0) * np.where(x.catch_crop, 0.85, 1.0) * rain_f)
    # N: farm N input (corrected survey / imputed) vs crop uptake
    from . import inventory as INV
    fd = INV.canonical()["farm_detail"]
    fd = fd[fd.year == year].set_index(["farm_id", "crop"])
    key = list(zip(x.farm_id, x.crop))
    n_in = pd.Series([fd.n_mineral_kg_ha.get(k, np.nan) + fd.n_manure_kg_ha.get(k, 0.0) for k in key], index=x.index)
    yld = pd.Series([fd.yield_t_ha.get(k, np.nan) for k in key], index=x.index)
    n_in = n_in.fillna(x.crop.map({c: v for c, v in [("pea", 25), ("potato", 95), ("carrot", 100), ("spinach", 130), ("turnip_rape", 95)]}))
    yld = yld.fillna(x.groupby("crop").crop.transform(lambda s: 1.0))
    surplus = (n_in - yld * x.crop.map(N_UPTAKE_PER_T)).clip(lower=0)
    x["n_surplus"] = surplus
    x["f_n"] = ((1 + surplus / 80) * np.where(x.soil_class_soilmap.isin(["sand", "fine_sand"]), 1.3, 1.0)
                * np.where(x.soil_class_soilmap == "organic", 1.4, 1.0) * np.where(x.drainage == "subsurface", 1.15, 1.0)
                * np.where(x.catch_crop, 0.75, 1.0) * rain_f)
    return x.reset_index(drop=True)


def _loads(x: pd.DataFrame, cat: pd.DataFrame, kp, lp, kn, ln) -> pd.DataFrame:
    ret = x.catchment_id.map(cat.set_index("catchment_id").nutrient_retention_frac_estimate)
    x = x.assign(p_kg=x.area_ha * P_EXPORT_KG_HA * kp * x.f_p * np.exp(-x.dist_to_water_m / lp) * (1 - ret),
                 n_kg=x.area_ha * N_EXPORT_KG_HA * kn * x.f_n * np.exp(-x.dist_to_water_m / ln) * (1 - ret))
    return x


@lru_cache(maxsize=1)
def calibration() -> dict:
    """Fit (scale, delivery length) for P and N on the dev catchments, in log space."""
    x = parcel_risk()
    cat = raw()["catchments"]
    dev = pd.read_csv(C.TASKS / "dev" / "D1_dev_catchments_2025.csv").set_index("catchment_id")
    best = {}
    for nut, col in [("p", "apetit_p_to_water_kg"), ("n", "apetit_n_to_water_kg")]:
        res = []
        for L in [100, 150, 200, 300, 400, 600, 800, 1200, 1600, 2500, 4000, 1e6]:
            y = _loads(x, cat, 1, L, 1, L).groupby("catchment_id")[f"{nut}_kg"].sum().reindex(dev.index)
            lk = np.log(dev[col]) - np.log(y)
            res.append((float(lk.std()), L, float(np.exp(lk.mean())), float(np.mean(np.abs(lk - lk.mean())) / np.log(10))))
        res.sort()
        sd, L, k, mae = res[0]
        best[nut] = dict(scale=k, delivery_length_m=L, dev_mae_log10=mae)
    return best


def d1_loads(year: int = 2025) -> pd.DataFrame:
    cal = calibration()
    cat = raw()["catchments"]
    x = _loads(parcel_risk(year), cat, cal["p"]["scale"], cal["p"]["delivery_length_m"], cal["n"]["scale"], cal["n"]["delivery_length_m"])
    return x


def d1_priority(year: int = 2025) -> pd.DataFrame:
    x = d1_loads(year)
    cat = raw()["catchments"].set_index("catchment_id")
    g = x.groupby("catchment_id").agg(apetit_p_to_water_kg=("p_kg", "sum"), apetit_n_to_water_kg=("n_kg", "sum"),
                                      apetit_area_ha=("area_ha", "sum"), parcels=("parcel_id", "count"))
    g = g.reindex(cat.index).fillna(0.0)
    p_med = g.apetit_p_to_water_kg[g.apetit_p_to_water_kg > 0].median()
    n_med = g.apetit_n_to_water_kg[g.apetit_n_to_water_kg > 0].median()
    g["state_weight"] = cat.ecological_status_class.map(STATE_WEIGHT)
    g["p_sensitivity"] = cat.p_sensitivity
    g["priority_score"] = (g.apetit_p_to_water_kg / p_med + 0.5 * g.apetit_n_to_water_kg / n_med) * g.state_weight * g.p_sensitivity
    g["priority_rank"] = g.priority_score.rank(ascending=False, method="first").astype(int)
    g["region"] = cat.region
    g["receiving_water_type"] = cat.receiving_water_type
    g["ecological_status_class"] = cat.ecological_status_class
    loads = raw()["catchment_loads"]
    l25 = loads[loads.year == year].set_index("catchment_id")
    g["monitored_p_load_kg"] = l25.total_p_load_kg
    g["monitored_n_load_kg"] = l25.total_n_load_kg
    g["apetit_share_of_monitored_p"] = g.apetit_p_to_water_kg / g.monitored_p_load_kg
    return g.reset_index().sort_values("priority_rank")


def d1_submission() -> pd.DataFrame:
    tpl = pd.read_csv(C.TASKS / "templates" / "D1_catchment_priority_submission.csv")
    g = d1_priority().set_index("catchment_id")
    tpl["apetit_p_to_water_kg"] = tpl.catchment_id.map(g.apetit_p_to_water_kg).round(2)
    tpl["apetit_n_to_water_kg"] = tpl.catchment_id.map(g.apetit_n_to_water_kg).round(1)
    tpl["priority_rank"] = tpl.catchment_id.map(g.priority_rank)
    return tpl


def parcel_hotspots(n: int = 25) -> pd.DataFrame:
    """Field-level actions: the parcels that deliver the most P per hectare, with the driver."""
    x = d1_loads()
    x["p_kg_per_ha"] = x.p_kg / x.area_ha
    x["action"] = np.select(
        [~x.buffer_strip & (x.dist_to_water_m < 100), x.p_test_filled > x.p_test_filled.quantile(0.8),
         ~x.catch_crop & x.crop.isin(["pea", "spinach"]), x.slope_pct > 4],
        ["Add buffer strip (L09/L10)", "Reduce P fertilisation; soil-P is high (L09)", "Catch crop after harvest (L10)",
         "Reduced tillage / winter cover on slope"], "Monitor")
    return x.sort_values("p_kg", ascending=False).head(n)[
        ["parcel_id", "farm_id", "catchment_id", "crop", "area_ha", "p_kg", "p_kg_per_ha", "n_kg", "dist_to_water_m",
         "p_test_filled", "slope_pct", "buffer_strip", "catch_crop", "action"]]


# ------------------------------------------------------------------------------ D2 screening
PRESSURES = {   # ENCORE-style pressure categories (0 none .. 3 high), by material group
    "oilseed": dict(land_use=3, water_use=1, nutrient_pollution=3, ghg=3, pesticides=2),
    "vegetable_contract": dict(land_use=2, water_use=2, nutrient_pollution=3, ghg=2, pesticides=2),
    "vegetable_bought": dict(land_use=2, water_use=3, nutrient_pollution=2, ghg=2, pesticides=2),
    "legume": dict(land_use=2, water_use=1, nutrient_pollution=1, ghg=1, pesticides=1),
    "protein": dict(land_use=3, water_use=2, nutrient_pollution=2, ghg=3, pesticides=2),
    "dairy": dict(land_use=3, water_use=2, nutrient_pollution=3, ghg=3, pesticides=1),
    "meat": dict(land_use=3, water_use=2, nutrient_pollution=3, ghg=3, pesticides=1),
    "cereal": dict(land_use=2, water_use=1, nutrient_pollution=2, ghg=2, pesticides=2),
    "oil": dict(land_use=2, water_use=2, nutrient_pollution=2, ghg=2, pesticides=2),
    "sauce": dict(land_use=1, water_use=3, nutrient_pollution=1, ghg=1, pesticides=2),
    "spice": dict(land_use=1, water_use=2, nutrient_pollution=1, ghg=1, pesticides=2),
    "sugar": dict(land_use=2, water_use=1, nutrient_pollution=2, ghg=1, pesticides=2),
    "fish": dict(land_use=0, water_use=0, nutrient_pollution=1, ghg=2, pesticides=0),
    "packaging": dict(land_use=1, water_use=1, nutrient_pollution=0, ghg=2, pesticides=0),
    "chemical": dict(land_use=0, water_use=1, nutrient_pollution=0, ghg=2, pesticides=0),
}
WATER_STRESS_LABEL = {0: "low", 1: "low-medium", 2: "medium-high", 3: "high", 4: "extremely high"}


def d2_screening(year: int = 2025) -> pd.DataFrame:
    from . import inventory as INV
    T = INV.canonical()
    lin = INV.lineage(T, year)
    goods = lin[lin.category == "cat01_purchased_goods"].copy()
    erp = T["purchases"][T["purchases"].year == year].set_index("record_key")
    goods["material_id"] = goods.line_item.str.split(":").str[0]
    contract = goods.line_item.str.startswith("contract_farming")
    crop_mat = {v: k for k, v in C.CROP_MATERIAL.items() if k != "M114"}
    goods.loc[contract, "material_id"] = goods.loc[contract, "line_item"].str.split(":").str[2].map(crop_mat)
    goods["origin_country"] = goods.record_key.map(erp.origin_country).fillna("FI")
    goods["t"] = np.where(goods.unit == "kg", goods.activity / 1000, np.nan)
    m = raw()["materials"].set_index("material_id")
    g = goods.groupby(["material_id", "origin_country"]).agg(tco2e=("tco2e", "sum"), t=("t", "sum")).reset_index()
    deliv = T["purchases"][(T["purchases"].year == year) & T["purchases"].material_id.isin(crop_mat.values())].groupby("material_id").qty_kg.sum() / 1000
    g.loc[g.material_id.isin(deliv.index), "t"] = g.material_id.map(deliv)
    g["material"] = g.material_id.map(m.name_en)
    g["group"] = g.material_id.map(m.material_group)
    g["sbtn_high_impact"] = g.material_id.map(m.sbtn_high_impact_commodity)
    g["eudr_commodity"] = g.material_id.map(m.eudr_commodity)
    on = raw()["origin_nature"].drop_duplicates("origin_country").set_index("origin_country")
    g["water_stress_cat"] = g.origin_country.map(on.baseline_water_stress_cat_indicative)
    g["deforestation_risk"] = g.origin_country.map(on.deforestation_conversion_risk_indicative).fillna("unknown")
    g["biodiversity_sensitivity"] = g.origin_country.map(on.biodiversity_sensitivity_indicative).fillna("unknown")
    g["nutrient_status"] = g.origin_country.map(on.freshwater_nutrient_status_indicative).fillna("unknown")
    for k in ["land_use", "water_use", "nutrient_pollution", "ghg", "pesticides"]:
        g[f"pressure_{k}"] = g.group.map(lambda x: PRESSURES.get(x, {}).get(k, 1))
    lvl = {"low": 0, "medium": 1, "high": 2, "very high": 3}
    state = (g.water_stress_cat.fillna(1) / 4 + g.biodiversity_sensitivity.map(lambda s: lvl.get(str(s).split(" ")[0], 1)) / 3
             + g.deforestation_risk.map(lambda s: lvl.get(str(s).split(" ")[0], 0)) / 2) / 3
    pressure = g[[c for c in g.columns if c.startswith("pressure_")]].sum(axis=1) / 15
    scale = np.log1p(g.tco2e) / np.log1p(g.tco2e.max())
    g["materiality_score"] = (100 * scale * (0.5 * pressure + 0.5 * state) * np.where(g.sbtn_high_impact, 1.25, 1.0)).round(1)
    g["why"] = [_why(r) for _, r in g.iterrows()]
    return g.sort_values("materiality_score", ascending=False)


def _why(r) -> str:
    out = []
    if r.sbtn_high_impact:
        out.append("SBTN high-impact commodity")
    if r.eudr_commodity:
        out.append("EUDR scope")
    if pd.notna(r.water_stress_cat) and r.water_stress_cat >= 3:
        out.append(f"water stress {WATER_STRESS_LABEL[int(r.water_stress_cat)]} in {r.origin_country}")
    if str(r.deforestation_risk).startswith(("medium", "high")):
        out.append(f"deforestation/conversion risk {r.deforestation_risk}")
    if str(r.nutrient_status).startswith("poor") or "eutroph" in str(r.nutrient_status):
        out.append(f"nutrient status: {r.nutrient_status}")
    return "; ".join(out) or "low indicative pressure × state"


@lru_cache(maxsize=1)
def catchments_geojson() -> dict:
    from . import store
    return json.loads(store.path("06_nature/catchments.geojson").read_text())
