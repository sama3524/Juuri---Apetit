## Method note

**Data sources.** Everything is computed from the Since AI synthetic package `APETIT_CLIMATE_NATURE_NAVIGATOR_2026` v1.0.0: raw feeds (ERP, meters, fuels, GoOs, refrigerants, waste, travel, commuting, shipments, PCFs), farm survey, Foodhills grower file, parcels and soil map, daily weather 1991–2060, catchments and monitored loads, scenarios, levers and the framework digest. No external data is used. All results describe a synthetic twin, not Apetit's actual emissions.

**Architecture (medallion).** *Bronze*: raw files, never modified. *Silver*: canonical tables = bronze + corrections whose review status allows them. *Gold*: inventory line items, targets, pathways, risk and nature indicators. Every gold number traces back through `lineage` (record keys, activity, factor, factor source) and the `audit_log` (record_key, field, old → new, rule, evidence, proposer, approver, status, timestamp).

**A · Data → inventory.**
* A rule-based data-quality agent with 13 rule groups. Examples: unit-price ratios per supplier (tonnes keyed as kg, decimal shifts), supplier-master material checks, supplier currency patterns, seasonality-aware meter spikes and gaps, GoO internal log vs national registry, the system history behind each refrigerant gas code, missing sea legs by material and origin, survey area vs declared parcels, soil map vs reported soil, coordinate sanity, and PCF unit and reference period.
* Legitimate oddities are deliberately not flagged: credit notes, vessel lots in tonnes, seasonal peaks and ammonia systems.
* The calculation is the package's reference calculator (GHG Protocol, AR6 GWP100).
* Contract farms use the corrected survey. The organic-soil share comes from the soil map, or from the survey when the grower reports organic soil. Non-respondents are imputed from their own history.
* GoOs come from the registry and count only for their own production year.
* Biogenic CO2 is reported as a memo item.

**B · Pathways.**
* The base year is 2025, structurally adjusted for Foodhills: full-year Bjuv plus about 300 Swedish pea growers. This is compared against a recalculated 2019 base (minus the grain trade, plus Foodhills).
* SBTi linear rates: Scope 1+2 4.2 %/yr, Scope 3 non-FLAG 2.5 %/yr, FLAG 3.03 %/yr.
* The business-as-usual (BAU) path follows the scenario's volume index. The Pudasjärvi closure moves pizza emissions from Scope 1/2 into category 1.
* Lever abatement is recomputed from the inventory where possible (L01, L03, L04, L07, L08, L11, L13, L20). The other levers keep their catalogue design estimates and are labelled as such.
* Costs are annualised (8 %, over lever lifetime) and net of avoided ETS2 fuel cost.
* Structural changes (L05, L26) and measurement changes (L27) are never counted as abatement.

**C · Climate risk.**
* One ridge model per crop, pooled across regions, on log yield.
* Features are agronomic stress indices from daily weather: water deficit by month and its interaction with irrigation, heat days, waterlogging after sowing on clay, harvest rain, season growing degree days (GDD), blight days, warm days in May for pollen beetle. Soil, irrigation, trend and a shrunken farm effect are added.
* Regularisation is chosen by leave-whole-seasons-out cross-validation.
* C2 holds 2025 management and the 2025 parcel mix fixed.
* C3 prices shortfalls as volume × contract price × (1 + import premium), and adds the emissions of replacement imports.

**D · Nature.**
* A field-to-water export-coefficient model: 1.1 kg P and 15 kg N per ha (SYKE averages), modulated by soil P, slope, clay, crop erosion risk, rain, buffer strips, catch crops, drainage and N surplus. Delivery decays exponentially with distance to water, then catchment retention applies.
* Two parameters per nutrient (scale and delivery length) are calibrated on the 6 dev catchments. The dev score is therefore in-sample.
* Priority follows the SBTN-style formula in the brief.
* D2 combines 2025 volumes and emissions by origin with indicative state of nature, ENCORE-style pressures and HIC/EUDR flags.

**E · Copilot.**
* Deterministic answers to E01–E39, each with evidence (record keys, file and line item, or framework section).
* An optional Claude agent (`claude-opus-5-5`, tool use) whose only tools are the platform's own functions. It is told to cite tool evidence and to say when data is missing.

**FIWARE live context.**
* Juuri publishes 3,238 NGSI-LD entities to Orion-LD, using Smart Data Models where one exists (AgriFarm, AgriParcel, AgriCrop, WeatherObserved, DeviceMeasurement, Building) and Juuri extensions where none does (Catchment, DataQualityIssue).
* Every result carries `source` and `reviewStatus` sub-properties.
* Two subscriptions drive the live rules: each meter reading is checked against its seasonal norm and can raise a DataQualityIssue for review; each weather observation updates the cell's season-to-date water balance, from which the yield model re-estimates `expectedYieldImpact` and `droughtAlert` on every parcel in that cell.
* The broker is optional: batch results never depend on it. See `fiware/README.md`.

**Expert review.**
* Proposals at confidence ≥ 0.7 are applied *provisionally*, and every page then shows a DRAFT badge.
* Reviewers approve or reject proposals (singly or in bulk by rule) on the Data quality page. Decisions are attributed and timestamped in `review/decisions.csv`.
* *Strict* mode applies approved corrections only, so a result is publishable only when no provisional corrections remain.

**Key assumptions & limitations.**
* The soil map is about 88 % accurate, so organic-soil emissions are an expected value. Growers should confirm their soil type.
* Meter gap-fills carry about ±7 % error (backtest).
* The yield model has only 9 Swedish seasons and 15 Finnish ones. Year-level weather effects are uncertain; see the backtest table.
* Lever costs and carbon prices are illustrative (NGFS-range values in the package).
* Category 10 is not calculated, and should be screened before an SBTi submission.
* Removals are not reported. Under LSR they require monitoring.

**Path to production on Apetit's stack.**

| Layer | Implementation |
|---|---|
| Storage | Fabric Lakehouse: bronze `Files/` → silver/gold Delta tables |
| Rules and calculation | Fabric notebooks running the `juuri` package |
| Issue and audit log, decisions | Delta tables, with a Power Apps approval flow |
| Dashboards | Power BI over the gold tables |
| Copilot | Azure AI Foundry agent whose tools are SQL over gold, the calculator and Azure AI Search over the framework PDFs |
| Geospatial | GeoParquet / Azure Maps with SYKE and Ruokavirasto layers |
