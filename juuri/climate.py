"""Track C: climate risk to contract farming.

Weather -> crop-stress features -> yield model per crop, learnt from 2012-2026 farm yields
(Finnish survey; Swedish Foodhills deliveries / area). The model is deliberately interpretable:
a ridge regression on agronomic stress indices (drought by stage, heat at flowering, waterlogging
after sowing, harvest rain, season warmth, blight and pollen-beetle pressure) plus farm, soil and
irrigation terms. Because the stress terms are physical, the model extrapolates sensibly to the
2041-2060 climates, where tree models would flatten out.

Outputs: C1 parcel yields 2027-2031, C2 risk 2041-2060 by SSP, C3 € exposure and adaptation value.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import config as C
from .data import raw

CROPS = ["pea", "potato", "carrot", "spinach", "turnip_rape"]
FEATURES = ["gdd_season", "t_may", "t_jul", "cwb_may", "cwb_jun", "cwb_jul", "cwb_aug", "dry_jun_jul", "hot_jun", "hot_jul",
            "hot_aug", "wet_after_sow", "rain_aug", "rain_sep", "blight_days", "beetle_may", "precip_season"]
SITE = ["clay_pct", "organic_share", "coarse_share", "irrigated", "trend"]


# ------------------------------------------------------------------------------ weather features
@lru_cache(maxsize=1)
def weather_features() -> pd.DataFrame:
    cache = C.OUTPUTS / "cache" / "weather_features.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    w = pd.read_parquet(C.DATA / "04_climate" / "weather_daily_1991_2060.parquet")
    w["year"] = w.date.dt.year
    w["month"] = w.date.dt.month
    w["doy"] = w.date.dt.dayofyear
    w["gdd"] = (w.tmean_c - 5).clip(lower=0)
    w["cwb"] = w.precip_mm - w.pet_mm
    w["hot"] = (w.tmax_c >= 27).astype(int)
    w["wetday"] = (w.precip_mm >= 1).astype(int)
    w["blight"] = ((w.tmean_c >= 15) & (w.precip_mm >= 0.5) & w.month.isin([7, 8])).astype(int)
    g = w.groupby(["cell_id", "scenario", "year"])
    m = w[w.month.between(5, 8)].groupby(["cell_id", "scenario", "year"])
    f = pd.DataFrame({
        "gdd_season": m.gdd.sum(),
        "precip_season": m.precip_mm.sum(),
        "t_may": w[w.month == 5].groupby(["cell_id", "scenario", "year"]).tmean_c.mean(),
        "t_jul": w[w.month == 7].groupby(["cell_id", "scenario", "year"]).tmean_c.mean(),
        "beetle_may": w[w.month == 5].groupby(["cell_id", "scenario", "year"]).tmax_c.apply(lambda x: (x >= 15).sum()),
        "wet_after_sow": w[w.doy.between(120, 160)].groupby(["cell_id", "scenario", "year"]).precip_mm.sum(),
        "rain_aug": w[w.month == 8].groupby(["cell_id", "scenario", "year"]).wetday.sum(),
        "rain_sep": w[w.month == 9].groupby(["cell_id", "scenario", "year"]).precip_mm.sum(),
        "blight_days": g.blight.sum(),
    })
    for mo, name in [(5, "may"), (6, "jun"), (7, "jul"), (8, "aug")]:
        sub = w[w.month == mo].groupby(["cell_id", "scenario", "year"])
        f[f"cwb_{name}"] = sub.cwb.sum()
        if mo in (6, 7, 8):
            f[f"hot_{name}"] = sub.hot.sum()
    # longest dry spell (days < 1 mm) in June-July
    jj = w[w.month.isin([6, 7])].sort_values(["cell_id", "scenario", "date"])

    def spell(x):
        best = cur = 0
        for v in x:
            cur = cur + 1 if v < 1 else 0
            best = max(best, cur)
        return best
    f["dry_jun_jul"] = jj.groupby(["cell_id", "scenario", "year"]).precip_mm.apply(lambda x: spell(x.values))
    f = f.reset_index()
    cache.parent.mkdir(parents=True, exist_ok=True)
    f.to_parquet(cache)
    return f


def _nearest_cell(lat, lon, region) -> np.ndarray:
    cells = raw()["climate_cells"]
    out = []
    for la, lo, rg in zip(lat, lon, region):
        c = cells[cells.region == rg] if (cells.region == rg).any() else cells
        d = (c.lat - la) ** 2 + ((c.lon - lo) * np.cos(np.radians(la))) ** 2
        out.append(c.cell_id.values[int(np.argmin(d.values))])
    return np.array(out)


@lru_cache(maxsize=1)
def parcels() -> pd.DataFrame:
    r = raw()
    p = r["parcels"].copy()
    fr = r["farm_registry"].set_index("farm_id")
    p["region"] = p.farm_id.map(fr.region)
    p["cell_id"] = _nearest_cell(p.lat, p.lon, p.region)
    p["organic_share"] = (p.soil_class_soilmap == "organic").astype(float)
    p["coarse_share"] = p.soil_class_soilmap.isin(["sand", "fine_sand"]).astype(float)
    p["clay_pct"] = p.clay_pct_soilmap
    return p


def _farm_site(year_col="year") -> pd.DataFrame:
    """Area-weighted soil per farm x crop x year from declared parcels."""
    ch = raw()["crop_history"]
    p = parcels().set_index("parcel_id")
    x = ch.join(p[["area_ha", "clay_pct", "organic_share", "coarse_share", "cell_id"]], on="parcel_id")
    for c in ["clay_pct", "organic_share", "coarse_share"]:
        x[c] = x[c] * x.area_ha
    g = x.groupby(["farm_id", "crop", "year"]).agg(area=("area_ha", "sum"), clay_pct=("clay_pct", "sum"),
                                                     organic_share=("organic_share", "sum"), coarse_share=("coarse_share", "sum"),
                                                     cell_id=("cell_id", lambda s: s.mode().iat[0]))
    for c in ["clay_pct", "organic_share", "coarse_share"]:
        g[c] = g[c] / g.area
    return g.reset_index()


# ------------------------------------------------------------------------------ training data
@lru_cache(maxsize=1)
def training_data() -> pd.DataFrame:
    r = raw()
    from . import canonical as K   # corrected survey (outliers / decimal slips fixed)
    T = K.build()
    sv = T["_tables"]["farm_survey"][["farm_id", "crop", "year", "yield_t_ha", "irrigated"]].copy()
    sv["region"] = "FI_SW"
    fh = r["foodhills"].rename(columns={"odlar_id": "farm_id", "skördeår": "year", "areal_ha": "area", "levererad_mängd_ton": "t"})
    fh = fh[fh.area > 0]
    fh = pd.DataFrame(dict(farm_id=fh.farm_id, crop="pea", year=fh.year, yield_t_ha=fh.t / fh.area,
                           irrigated=fh.bevattnad.str.lower().eq("ja"), region="SE_SKANE"))
    d = pd.concat([sv, fh], ignore_index=True)
    site = _farm_site()
    d = d.merge(site, on=["farm_id", "crop", "year"], how="left")
    fr = raw()["farm_registry"].set_index("farm_id")
    miss = d.cell_id.isna()
    d.loc[miss, "cell_id"] = _nearest_cell(d.loc[miss, "farm_id"].map(fr.lat), d.loc[miss, "farm_id"].map(fr.lon), d.loc[miss, "region"])
    for c in ["clay_pct", "organic_share", "coarse_share"]:
        d[c] = d[c].fillna(d.groupby("region")[c].transform("median"))
    wf = weather_features()
    d = d.merge(wf[wf.scenario == "historical"].drop(columns="scenario"), on=["cell_id", "year"], how="left")
    d["irrigated"] = d.irrigated.astype(float)
    d["trend"] = d.year - 2020
    return d.dropna(subset=["yield_t_ha", "gdd_season"])


CROP_FEATURES = {   # agronomic responses named in docs/SCENARIOS_AND_RISK.md
    "pea": ["cwb_jun_deficit", "cwb_jul_deficit", "cwb_jun_deficit_x_irr", "hot_jul", "wet_after_sow_x_clay", "rain_aug", "gdd_season", "gdd_sq"],
    "turnip_rape": ["cwb_jun_deficit", "hot_jul", "beetle_may", "rain_aug", "gdd_season"],
    "potato": ["cwb_jul_deficit", "cwb_aug_deficit", "cwb_jul_deficit_x_irr", "blight_days", "hot_jul", "rain_sep", "gdd_season", "gdd_sq"],
    "carrot": ["cwb_jul_deficit", "cwb_aug_deficit", "cwb_jul_deficit_x_irr", "hot_jul", "rain_sep", "gdd_season", "gdd_sq"],
    "spinach": ["cwb_may_deficit", "cwb_jun_deficit", "cwb_jun_deficit_x_irr", "hot_jun", "wet_after_sow_x_clay", "gdd_season"],
}
ALPHAS = np.logspace(0, 4, 13)


def _design(df: pd.DataFrame, crop: str) -> pd.DataFrame:
    """Hinge/stress transforms so the linear model captures thresholds and extrapolates physically."""
    X = pd.DataFrame(index=df.index)
    for c in ["cwb_may", "cwb_jun", "cwb_jul", "cwb_aug"]:
        X[f"{c}_deficit"] = (-df[c]).clip(lower=0)
        X[f"{c}_deficit_x_irr"] = X[f"{c}_deficit"] * df.irrigated
    X["wet_after_sow_x_clay"] = df.wet_after_sow * df.clay_pct / 50
    X["gdd_sq"] = (df.gdd_season - 1300) ** 2 / 1e4
    for c in ["hot_jun", "hot_jul", "beetle_may", "t_may", "rain_aug", "rain_sep", "blight_days", "gdd_season"]:
        X[c] = df[c]
    X = X[CROP_FEATURES[crop]].copy()
    for c in ["clay_pct", "organic_share", "coarse_share", "irrigated", "trend"]:
        X[c] = df[c].astype(float)
    X["region_se"] = (df.region == "SE_SKANE").astype(float)
    return X


def _fit_ridge(X, y, years):
    """Choose alpha by leave-whole-seasons-out CV: weather varies by season, so row-wise CV leaks."""
    from sklearn.linear_model import Ridge
    uy = np.array(sorted(set(years)))
    folds = np.array_split(np.random.default_rng(0).permutation(uy), min(5, len(uy)))
    best, best_err = ALPHAS[0], np.inf
    for a in ALPHAS:
        err = 0.0
        for f in folds:
            te = np.isin(years, f)
            m = make_pipeline(StandardScaler(), Ridge(alpha=a)).fit(X[~te], y[~te])
            err += ((m.predict(X[te]) - y[te]) ** 2).sum()
        if err < best_err:
            best, best_err = a, err
    return make_pipeline(StandardScaler(), Ridge(alpha=best)).fit(X, y), best


class YieldModel:
    """One model per crop, pooled across regions (shared weather response, regional level)."""

    def __init__(self):
        self.models, self.farm_effect, self.metrics = {}, {}, {}

    def fit(self, d: pd.DataFrame, max_year: int | None = None):
        if max_year:
            d = d[d.year <= max_year]
        for crop, g in d.groupby("crop"):
            if len(g) < 40:
                continue
            X, y = _design(g, crop), np.log(g.yield_t_ha.clip(lower=0.05)).values
            m, alpha = _fit_ridge(X, y, g.year.values)
            resid = pd.Series(y - m.predict(X), index=g.index)
            fe = resid.groupby(g.farm_id).agg(["sum", "count"])
            self.farm_effect[crop] = (fe["sum"] / (fe["count"] + 3)).to_dict()
            self.models[crop] = m
            self.metrics[crop] = dict(n=len(g), resid_sd=float(resid.std()), alpha=float(alpha))
        return self

    def predict(self, df: pd.DataFrame, use_farm=True) -> np.ndarray:
        out = np.full(len(df), np.nan)
        for crop, idx in df.groupby("crop").groups.items():
            m = self.models.get(crop)
            if m is None:
                continue
            g = df.loc[idx]
            lp = m.predict(_design(g, crop))
            if use_farm:
                lp = lp + g.farm_id.map(self.farm_effect[crop]).fillna(0.0).values
            out[df.index.get_indexer(idx)] = np.exp(lp)
        return out

    def coefficients(self, crop) -> pd.Series:
        m = self.models[crop]
        cols = _design(training_data()[training_data().crop == crop].head(2), crop).columns
        return pd.Series(m[-1].coef_, index=cols).sort_values()


@lru_cache(maxsize=1)
def model() -> YieldModel:
    return YieldModel().fit(training_data())


@lru_cache(maxsize=4)
def backtest(split_year: int = 2021) -> pd.DataFrame:
    """Train on <= split_year, test on later seasons; compare with the farm-mean climatology baseline."""
    d = training_data().reset_index(drop=True)
    m = YieldModel().fit(d, max_year=split_year)
    test = d[d.year > split_year].copy()
    test["pred"] = m.predict(test)
    clim = d[d.year <= split_year].groupby(["farm_id", "crop"]).yield_t_ha.mean()
    test["clim"] = [clim.get((f, c), np.nan) for f, c in zip(test.farm_id, test.crop)]
    test["clim"] = test.clim.fillna(test.groupby("crop").clim.transform("mean"))
    rows = []
    for (crop, region), g in test.groupby(["crop", "region"]):
        g = g.dropna(subset=["pred"])
        agg = g.groupby("year")[["yield_t_ha", "pred", "clim"]].mean()
        rows.append(dict(crop=crop, region=region, n=len(g),
                         model_mape_yearly=float((abs(agg.pred - agg.yield_t_ha) / agg.yield_t_ha).mean()),
                         clim_mape_yearly=float((abs(agg.clim - agg.yield_t_ha) / agg.yield_t_ha).mean()),
                         model_nrmse=float(np.sqrt(((g.pred - g.yield_t_ha) ** 2).mean()) / g.yield_t_ha.mean()),
                         clim_nrmse=float(np.sqrt(((g.clim - g.yield_t_ha) ** 2).mean()) / g.yield_t_ha.mean())))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------ C1: 2027-2031
def _parcel_frame(rows: pd.DataFrame, scenario: str, year_col="year", trend_year=None) -> pd.DataFrame:
    p = parcels().set_index("parcel_id")
    x = rows.join(p[["cell_id", "clay_pct", "organic_share", "coarse_share"]], on="parcel_id")
    irr = training_data().groupby(["farm_id", "crop"]).irrigated.mean()
    x["irrigated"] = [float(irr.get((f, c), 0.0) > 0.5) for f, c in zip(x.farm_id, x.crop)]
    wf = weather_features()
    wf = wf[wf.scenario == scenario].drop(columns="scenario")
    x = x.merge(wf, left_on=["cell_id", year_col], right_on=["cell_id", "year"], how="left", suffixes=("", "_w"))
    x["trend"] = (x[year_col] if trend_year is None else trend_year) - 2020
    return x


def c1_predictions() -> pd.DataFrame:
    tpl = pd.read_csv(C.TASKS / "templates" / "C1_yields_2027_2031_submission.csv")
    x = _parcel_frame(tpl.drop(columns=["yield_t_ha_pred"]), "ssp245")
    x["yield_t_ha_pred"] = np.round(model().predict(x.reset_index(drop=True)), 3)
    return tpl.drop(columns=["yield_t_ha_pred"]).assign(yield_t_ha_pred=x.yield_t_ha_pred.values)


# ------------------------------------------------------------------------------ C2: 2041-2060
def _mix_2025() -> pd.DataFrame:
    ch = raw()["crop_history"]
    mix = ch[(ch.year == 2025) & ch.crop.isin(CROPS)][["parcel_id", "farm_id", "crop"]]
    fr = raw()["farm_registry"].set_index("farm_id")
    mix["region"] = mix.farm_id.map(fr.region)
    se = raw()["parcels"]
    se_mix = ch[(ch.year == 2025) & ch.farm_id.str.startswith("S") & (ch.crop == "pea")][["parcel_id", "farm_id", "crop"]]
    se_mix["region"] = "SE_SKANE"
    return pd.concat([mix[mix.region == "FI_SW"], se_mix]).drop_duplicates("parcel_id")


@lru_cache(maxsize=1)
def climate_yields() -> pd.DataFrame:
    """Area-weighted mean yield per region x crop x scenario x year (2025 parcel mix, 2025 management)."""
    mix = _mix_2025()
    p = parcels().set_index("parcel_id")
    mix["area_ha"] = mix.parcel_id.map(p.area_ha)
    out = []
    for scen, years in [("historical", range(1991, 2027)), ("ssp126", range(2027, 2061)), ("ssp245", range(2027, 2061)),
                        ("ssp585", range(2027, 2061))]:
        rows = pd.concat([mix.assign(year=y) for y in years], ignore_index=True)
        x = _parcel_frame(rows, scen, trend_year=2025).reset_index(drop=True)
        x["yield"] = model().predict(x)
        x["prod"] = x["yield"] * x.area_ha
        g = x.groupby(["region", "crop", "year"]).agg(prod=("prod", "sum"), area=("area_ha", "sum")).reset_index()
        g["yield_t_ha"] = g["prod"] / g.area
        g["scenario"] = scen
        out.append(g)
    return pd.concat(out, ignore_index=True)


def c2_risk() -> pd.DataFrame:
    cy = climate_yields()
    base = cy[(cy.scenario == "historical") & cy.year.between(1991, 2020)].groupby(["region", "crop"]).yield_t_ha.mean()
    rows = []
    for scen in ["ssp126", "ssp245", "ssp585"]:
        fut = cy[(cy.scenario == scen) & cy.year.between(2041, 2060)]
        for (rg, cr), g in fut.groupby(["region", "crop"]):
            b = base[(rg, cr)]
            rows.append(dict(region=rg, crop=cr, scenario=scen, period="2041-2060",
                             mean_yield_change_pct=round(100 * (g.yield_t_ha.mean() / b - 1), 2),
                             p_bad_year=round(float((g.yield_t_ha < 0.8 * b).mean()), 3), baseline_t_ha=b,
                             future_t_ha=g.yield_t_ha.mean()))
    base_bad = cy[(cy.scenario == "historical") & cy.year.between(1991, 2020)].groupby(["region", "crop"]).apply(
        lambda g: (g.yield_t_ha < 0.8 * g.yield_t_ha.mean()).mean(), include_groups=False)
    df = pd.DataFrame(rows)
    df["p_bad_year_1991_2020"] = [base_bad[(r, c)] for r, c in zip(df.region, df.crop)]
    return df


def c2_submission() -> pd.DataFrame:
    tpl = pd.read_csv(C.TASKS / "templates" / "C2_climate_yield_risk_submission.csv")
    r = c2_risk().set_index(["region", "crop", "scenario"])
    tpl["mean_yield_change_pct"] = [r.loc[(a, b, c), "mean_yield_change_pct"] for a, b, c in zip(tpl.region, tpl.crop, tpl.scenario)]
    tpl["p_bad_year"] = [r.loc[(a, b, c), "p_bad_year"] for a, b, c in zip(tpl.region, tpl.crop, tpl.scenario)]
    return tpl


# ------------------------------------------------------------------------------ C3: € risk
def c3_euro_risk() -> pd.DataFrame:
    """Expected annual cost of shortfalls: shortfall t x contract price x replacement-import premium,
    plus the emissions of replacement imports. Shortfall = max(0, plan volume - harvest) per year."""
    cy = climate_yields()
    pr = raw()["prices"]
    pr = pr[pr.year == pr.year.max()].set_index("item")
    T = __import__("juuri.inventory", fromlist=["x"]).canonical()
    ef = T["ef_library"].set_index("ef_id").kgco2e_per_unit
    import_ef = {"pea": ef.get("EF-FROZ-CAUL-NL", 1.0), "potato": ef.get("EF-FROZ-CAUL-NL", 1.0), "carrot": ef.get("EF-FROZ-CAUL-NL", 1.0),
                 "spinach": ef.get("EF-FROZ-CAUL-NL", 1.0), "turnip_rape": ef.get("EF-RAP-PL", 1.3)}
    base = cy[(cy.scenario == "historical") & cy.year.between(1991, 2020)].groupby(["region", "crop"])["prod"].mean()
    rows = []
    for scen in ["historical", "ssp126", "ssp245", "ssp585"]:
        per = (1996, 2025) if scen == "historical" else (2041, 2060)
        g0 = cy[(cy.scenario == scen) & cy.year.between(*per)]
        for (rg, cr), g in g0.groupby(["region", "crop"]):
            plan = base[(rg, cr)]
            short = (plan - g["prod"]).clip(lower=0)
            price = pr.loc[cr, "contract_price_eur_t"] if cr in pr.index else pr.contract_price_eur_t.mean()
            prem = pr.loc[cr, "replacement_import_premium_frac"] if cr in pr.index else 0.4
            rows.append(dict(region=rg, crop=cr, scenario=scen, period=f"{per[0]}-{per[1]}", plan_t=plan,
                             expected_shortfall_t=short.mean(), p95_shortfall_t=short.quantile(0.95),
                             expected_cost_eur=float((short * price * (1 + prem)).mean()),
                             p95_cost_eur=float((short * price * (1 + prem)).quantile(0.95)),
                             premium_cost_eur=float((short * price * prem).mean()),
                             replacement_tco2e=float((short * import_ef[cr]).mean())))
    return pd.DataFrame(rows)


def adaptation_irrigation(crop="potato", region="FI_SW", scenario="ssp585") -> dict:
    """Value of irrigation: yield with vs without irrigation on the 2025 mix, 2041-2060."""
    mix = _mix_2025()
    mix = mix[(mix.crop == crop) & (mix.region == region)]
    p = parcels().set_index("parcel_id")
    mix["area_ha"] = mix.parcel_id.map(p.area_ha)
    rows = pd.concat([mix.assign(year=y) for y in range(2041, 2061)], ignore_index=True)
    x = _parcel_frame(rows, scenario, trend_year=2025).reset_index(drop=True)
    y0 = model().predict(x.assign(irrigated=0.0))
    y1 = model().predict(x.assign(irrigated=1.0))
    gain = float(((y1 - y0) * x.area_ha).sum() / 20)
    return dict(crop=crop, region=region, scenario=scenario, yield_gain_t_per_yr=gain,
                gain_pct=float(100 * (y1 * x.area_ha).sum() / max((y0 * x.area_ha).sum(), 1e-9) - 100))
