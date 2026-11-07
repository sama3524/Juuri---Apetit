# Juuri: Climate & Nature Navigator

*Juuri* is Finnish for "root". Every number in the platform traces back to its root: the source record, the emission factor and the reviewed correction behind it.

Juuri is a decision platform for Apetit's climate and nature work, built for the Apetit × Since AI Hackathon 2026. It reads the raw, error-laden data feeds and goes through five steps:

1. Finds and proposes fixes for data-quality issues, and has a person approve them.
2. Produces an auditable GHG inventory with a FLAG split.
3. Sets SBTi-aligned targets and tests pathways and a marginal abatement cost curve (MACC) under transition scenarios.
4. Forecasts climate risk to contract farming in tonnes and euros.
5. Ranks catchments and sourcing origins by nature impact (SBTN).

It ships as a **web product**: a FastAPI backend and a single-page web app, available in **Finnish, Swedish and English**. Its built-in **assistant** lets users ask questions about the data and request reports. Reports are generated from the reviewed data, open as printable pages (Save as PDF) and download as Markdown.

A **FIWARE live context layer** (Orion-LD, NGSI-LD, Smart Data Models) makes it live: weather and meter observations stream into the broker, and Juuri reacts. It raises data-quality issues for review, updates the drought impact on every contract parcel, and publishes its results back as context.

> All data is the synthetic twin supplied by Since AI. Results are not Apetit's actual emissions and not facts about real farms.

## Quick start

Requires Python 3.9 or later. Install and run. In every new terminal, run `source .venv/bin/activate` first: macOS has no bare `python` command outside the virtual environment.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m juuri setup     # unzips Data/APETIT_CLIMATE_NATURE_NAVIGATOR_2026.zip into package/
python -m juuri run       # raw → reviewed → results; writes outputs/ and outputs/submissions/ (~20 s)
python -m juuri score     # scores against the public dev answer keys
python -m juuri serve     # the web product at http://localhost:8000  (API docs at /api/docs)
```

Open http://localhost:8000 and use the FI / SV / EN switch in the top bar. To open a page in a given language, add `?lang=fi` or `?lang=sv` to the URL, for example http://localhost:8000/?lang=fi#/assistant.

The assistant works in two modes:

- **Claude agent:** set a credential with `export ANTHROPIC_API_KEY=...` before running `serve`. Claude (`claude-opus-5-5`) answers in the user's language. It uses only Juuri's own tools (inventory, lineage, issue log, pathways, climate risk, catchments, framework documents and the live broker), cites the evidence, and generates standard or custom reports.
- **Offline (no key):** a multilingual intent router answers from the same data and generates the same standard reports, so the product works fully without a key.

For the optional live context layer (needs Docker):

```bash
python -m juuri fiware up      # Orion-LD context broker + MongoDB
python -m juuri fiware load    # 3,238 NGSI-LD entities (Smart Data Models) + results + subscriptions
python -m juuri fiware demo    # notification listener + live simulator (2026 season, raw 2025 meter feed)
```

The batch pipeline never needs the broker. Details are in [fiware/README.md](fiware/README.md).

## What it does

| Track | What Juuri delivers | Where |
|---|---|---|
| **A** Data → inventory | A data-quality agent with 13 rule groups, covering unit slips, duplicates, currency, material codes, meter spikes and gaps, GoO vs registry, refrigerant gas codes, missing sea legs, survey area and soil, coordinates, and PCF unit and period. Proposals go to an expert-review queue, then to an auditable 2025 inventory with record-level lineage and a FLAG split. | Data quality, GHG inventory |
| **B** Pathways | Base-year options (2019 recalculated vs 2025 structurally adjusted for Foodhills), SBTi targets for Scope 1+2, Scope 3 non-FLAG and FLAG, BAU vs lever pathways under two NGFS scenarios, a MACC with uncertainty bands, owners per lever, and a Scope 3 coverage check | Targets & pathways |
| **C** Climate risk | An interpretable yield model built on weather stress indices; 2027–2031 parcel forecasts; 2041–2060 yield change and bad-year probability by SSP; € shortfall exposure; the value of irrigation; the FI + SE pea sourcing hedge | Climate risk |
| **D** Nature | A field-to-water P/N model and SBTN-style catchment priority; a map; field-level actions for advisors; value-chain materiality screening by material and origin | Nature (SBTN) |
| **E** Assistant | A chat assistant in FI/SV/EN that answers from the data with an evidence trail and produces reports on request ("tee raportti tilasta F0123", "gör en klimatriskrapport"). E01–E39 are answered with evidence. The Claude tool-use agent is restricted to Juuri's own data, documents and the live broker. | Assistant |
| **Reports** | 8 standard reports (executive summary, inventory, data quality, targets, climate risk, nature, farm profile, catchment profile) plus custom reports composed by the assistant. All are localised, carry DRAFT/approved status and sources, and render as printable HTML with inline charts, or as Markdown. | Reports |
| **FIWARE** live context | Orion-LD with 8 entity types: AgriFarm, AgriParcel, AgriCrop, WeatherObserved, DeviceMeasurement and Building from Smart Data Models, plus Catchment and DataQualityIssue as Juuri extensions. Two subscriptions drive live data-quality checks and drought re-estimation. Results carry `source` and `reviewStatus` sub-properties. | Live context (FIWARE) |

### Expert review is built in

The agent **proposes** and a person **decides**. Each proposal carries its rule, evidence, old and new value, and a confidence score.

- **Provisional mode (default):** proposals with confidence ≥ 0.7 are applied, and every page shows a **DRAFT** badge until a reviewer approves them.
- **Strict mode:** only approved corrections are applied.

Reviewers approve or reject proposals one at a time, or in bulk by rule. Decisions are written, attributed and timestamped, to `review/decisions.csv`. That file is the only input besides the raw package, so any result can be reproduced from the raw data plus the decisions.

The draft 2025 report shows Scope 1 as **132,163 t**. That is exactly what the uncorrected raw data gives, so the draft was built from uncorrected data. After review, Scope 1 is **4,414 t**.

## Results on the public dev keys

These are scores from `python -m juuri score`, using the package's own scorer.

| Task | Juuri | Package baseline |
|---|---|---|
| A1 data-quality issues, 2023–2024 (F1-based) | **95.6** | 52.2 |
| A2 inventory 2023 | **92.6** | 25.5 |
| A2 inventory 2024 | **91.5** | 23.2 |
| E numeric dev questions | **100** | – |
| D1 catchments (6 dev catchments; in-sample, since they are also the calibration set) | 65.6 | 12.0 |
| C1 backtest: train ≤ 2019, test 2020–2026 | beats the farm-mean climatology on yearly error for most crops | see the Climate risk page |

The 2025 inventory (provisional) comes out at:

| Scope | t CO2e |
|---|---|
| Scope 1 | 4,414 |
| Scope 2, market-based | 61 |
| Scope 2, location-based | 963 |
| Scope 3 | 227,317 |

FLAG emissions are about 70 % of the total, so a FLAG target is mandatory. Drained organic soils cover about 6 % of contract area but cause about 57 % of Finnish contract-farm emissions. Today's calculator, which assumes mineral soil, misses them.

## Outputs

`python -m juuri run` writes the following:

| Path | Content |
|---|---|
| `outputs/submissions/` | `A1_issues_submission.csv`, `A2_inventory_2025_submission.csv`, `C1_yields_2027_2031_submission.csv`, `C2_climate_yield_risk_submission.csv`, `D1_catchment_priority_submission.csv`, `E_answers_submission.csv` |
| `outputs/audit_log.csv`, `issues_all_years.csv` | Every correction: record key, field, old → new, rule, evidence, status, approver |
| `outputs/lineage_2025.csv` | Source records behind each inventory line item |
| `outputs/inventory_2021_2025.csv`, `flag_split_2025.csv` | Inventory by year and the FLAG split |
| `outputs/B_*.csv` | Base-year options, targets, levers, pathways, MACC |
| `outputs/C3_euro_risk.csv`, `C_backtest_2020_2026.csv` | € risk and model backtest |
| `outputs/D1_*.csv`, `D2_materiality_screening.csv` | Catchment priority, parcel hotspots, value-chain screening |

## Repository layout

```
juuri/                 platform package
  data.py              bronze: load raw package, attach record keys
  quality.py           data-quality agent (proposes issues + corrections)
  review.py            expert decisions, provisional/strict policy
  canonical.py         silver: apply corrections, audit log, contract-farm footprints
  inventory.py         gold: GHG inventory, lineage, FLAG split
  pathways.py          SBTi targets, base years, scenario pathways, MACC
  climate.py           weather features, yield model, C1/C2/C3
  nature.py            P/N loads, catchment priority, materiality screening
  copilot.py           evidence answers, document search, Claude agent
  fiware.py            NGSI-LD client, Smart Data Models entities, subscriptions, live engine, simulator
  api.py               FastAPI backend: JSON API + serves the web app
  assistant.py         chat: Claude agent with report tools, or offline multilingual intent router
  reports.py           report engine: standard + custom reports, HTML (inline SVG charts) and Markdown
  i18n.py              translations and locale formatting (shared with the web app)
web/                   single-page web app (no build step): index.html, app.js, styles.css
  i18n/                en.json, fi.json, sv.json (UI + report text), catalogue.json (lever/owner names)
legacy/streamlit_app/  earlier Streamlit prototype, kept for reference only
fiware/                docker-compose.yml (Orion-LD + MongoDB) and the FIWARE integration guide
docs/METHOD_NOTE.md    one-page method note: sources, assumptions, limitations, review
review/decisions.csv   reviewer decisions (created on first decision)
Data/                  challenge brief and data package zip (input)
```

The calculation reuses the package's MIT-licensed reference calculator and scorer (`package/.../tools/ccn_tools`), so the accounting method matches the one used for the answer keys. Juuri's own work is in what goes into the calculator (cleaned, reviewed data) and what comes out (lineage, review, decisions).

## Path to Apetit's Microsoft stack

| Layer | Implementation |
|---|---|
| Storage | Fabric Lakehouse: raw files in `Files/` (bronze), canonical and result tables as Delta (silver/gold) |
| Rules and calculation | The `juuri` package runs unchanged in Fabric notebooks |
| Issue log, audit log, decisions | Delta tables; reviewer approvals through Power Apps |
| Web product | The FastAPI app runs on Azure App Service or Container Apps behind Entra ID; Power BI can sit on the same gold tables |
| Copilot | Azure AI Foundry agent with the same tools: SQL on gold tables, the calculator, Azure AI Search over the framework PDFs |
| Geospatial | GeoJSON/GeoParquet layers, so real SYKE catchments and Ruokavirasto parcels drop in without code changes |
| Live context | Orion-LD (NGSI-LD) as the open interoperability layer: growers' sensors, cooperatives and public data publish Smart Data Models entities through IoT Agents, and a subscription-fed pipeline syncs the broker to Fabric |

## Limitations

- The soil map is about 88 % accurate, so organic-soil emissions are an expected value. Confirm soil type with the flagged growers.
- Lever costs and carbon prices are illustrative values within NGFS ranges.
- The yield model has only 9 Swedish and 15 Finnish seasons, so weather effects are uncertain. The backtest is shown in the app.
- Scope 3 category 10 and removals are not calculated.

See [docs/METHOD_NOTE.md](docs/METHOD_NOTE.md) for the full method note.

## Licences

- Package data: CC BY 4.0, © Since AI ry. The exception is `data/00_apetit_original/`, which is shared by Apetit for the hackathon only and is not CC BY.
- Package tools: MIT.
