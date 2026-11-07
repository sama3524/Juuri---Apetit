# Juuri × FIWARE: live context layer

Juuri uses a FIWARE **Orion-LD** context broker as its live data layer. Farms, parcels, plants, weather stations, meters and catchments are **NGSI-LD** entities, modelled with **Smart Data Models** wherever one exists. New observations stream into the broker, Juuri reacts to subscription notifications, and it writes its decisions back as context. That way every consumer sees the same live, traceable picture: the app, the copilot, Power BI or a grower's system.

```
 weather stations, energy meters ──NGSI-LD──▶  Orion-LD  ──notifications──▶  Juuri live engine
 (simulated: 2026 season, raw 2025 feed)        (Smart Data Models)              │ data-quality check
                                                     ▲                           │ drought impact (yield model)
          Juuri batch results (inventory, P/N, ──────┘◀──── DataQualityIssue, AgriParcel.expectedYieldImpact
          priority, farm footprints)                      ▲
                                      web app "Live context" page · assistant tool `query_context_broker`
```

## Run it

```bash
python -m juuri fiware up        # Orion-LD + MongoDB (docker compose, fiware/docker-compose.yml)
python -m juuri fiware load      # 3,238 entities + results + 2 subscriptions (~20 s)
python -m juuri fiware demo      # notification listener + live simulator (full season ≈ 10 min)
#   options: python -m juuri fiware demo start=2026-06-01 days=14 pace=0.03
python -m juuri fiware status    # entity counts, subscription delivery, drought alerts
python -m juuri serve            # web app page "Live context (FIWARE)"
python -m juuri fiware down
```

You can also run `listen` and `simulate` in separate terminals. The web app page can push the simulation forward 7 days at a time, as long as a listener is running.

## Entity model

| Entity type | Smart Data Model | Count | Key attributes |
|---|---|---|---|
| `Building` | [Building](https://github.com/smart-data-models/dataModel.Building) | 5 | location, category, `ghgScope1`, `ghgScope2Market` (2025, with source and reviewStatus) |
| `AgriFarm` | [AgriFarm](https://github.com/smart-data-models/dataModel.Agrifood) | 680 | location (coordinates after correction), `hasAgriParcel`, `farmEmissions`, `flagEmissions`, `organicSoilShare` |
| `AgriParcel` | [AgriParcel](https://github.com/smart-data-models/dataModel.Agrifood) | 2,410 (2025 contract crops) | Polygon location, area, `belongsTo` → AgriFarm, `hasAgriCrop` → AgriCrop, `refCatchment`, `refWeatherCell`, `phosphorusToWater`, `nitrogenToWater`, live `waterDeficitToDate`, `expectedYieldImpact`, `droughtAlert` |
| `AgriCrop` | [AgriCrop](https://github.com/smart-data-models/dataModel.Agrifood) | 5 | `yieldChange2041_2060`, `badYearProbability2041_2060` (SSP2-4.5) |
| `WeatherObserved` | [WeatherObserved](https://github.com/smart-data-models/dataModel.Weather) | 84 grid cells | live dateObserved, temperature, max/min temperature, precipitation, solarRadiation, potential evapotranspiration |
| `DeviceMeasurement` | [DeviceMeasurement](https://github.com/smart-data-models/dataModel.Device) | 12 site meters | live numValue (MWh), dateObserved, `refBuilding` |
| `Catchment` | *Juuri extension*: no SDM exists for WFD catchments | 42 | Polygon location, ecologicalStatus, receivingWaterType, pSensitivity, P/N to water, priorityRank |
| `DataQualityIssue` | *Juuri extension* | created live | issueType, recordKey, observedValue, proposedValue, confidence, rule, explanation, `reviewStatus`, `refMeasurement` |

**@context.** Juuri hosts one context in the broker (`POST /ngsi-ld/v1/jsonldContexts`). It imports the unified Smart Data Models context, `https://smartdatamodels.org/context.jsonld`, and adds Juuri's terms under `https://github.com/juuri-navigator/ns#`. Types and attributes therefore expand to the official SDM IRIs; for example, `AgriParcel` expands to `https://uri.fiware.org/ns/data-models#AgriParcel`.

**Traceability in the broker.** Every result is an NGSI-LD Property with sub-properties `source` (record keys, files, model), `method` and `reviewStatus` (provisional or approved), plus `observedAt` and a `unitCode` (TNE, KGM, HAR, MMT, MWH).

## Live rules

| Subscription | Watches | Juuri reaction |
|---|---|---|
| `urn:ngsi-ld:Subscription:juuri-meters` | `DeviceMeasurement.dateObserved` | Compares the reading with the 2021–2024 median for the same site, carrier and month. Above 200× → `unit_error` (kWh in the MWh field); above 2.5× → `meter_spike`; below 0.2× → implausible. Creates a `DataQualityIssue` (pending review) with the **same record key** the batch agent uses, so one decision covers both. |
| `urn:ngsi-ld:Subscription:juuri-weather` | `WeatherObserved.dateObserved` | Accumulates the water balance (P − PET) and heat days per cell since 1 May. Every week it re-runs Juuri's yield model for each parcel in that cell: season to date as observed, the rest of the season at climatology, compared with 1991–2020. It updates `expectedYieldImpact` and `droughtAlert` (≤ −7 % moderate, ≤ −15 % high) on the AgriParcel entities. |

The listener logs every reaction to `outputs/live/events.jsonl`. Approving or rejecting a live issue in the app PATCHes its `reviewStatus` in the broker and appends to `review/decisions.csv`.

## Compatibility notes

These are things found while building against Orion-LD 1.15.0-next (the image is pinned by digest):

- **No arm64 image.** On Apple silicon the broker runs under amd64 emulation (`platform: linux/amd64`).
- **`@type: @id` coercion.** The unified SDM context declares `belongsTo`, `hasAgriCrop` and `refBuilding` with `"@type": "@id"`, and Orion-LD then rejects them as NGSI-LD Relationships. Juuri's context re-declares those terms with the **same IRIs** and no coercion. This only works if Juuri's terms come *first* in the context array, because Orion-LD resolves terms first-match.
- **PATCH only updates existing attributes.** `PATCH /entities/{id}/attrs` doesn't create new ones, so live observations use `POST /entities/{id}/attrs`.
- **Notifications are lossy under bursts.** Orion-LD (at least under emulation) does not reliably notify for every entity in a batch upsert or a rapid burst of updates. Juuri therefore has each station post its own observation, paced like real devices, and runs the broker with `-notificationMode threadpool`. The engine also gap-fills any missed day with the cell's climatological water balance and counts those days (`gap_filled_days` in the event log).
- **Notifications only fire on change.** A subscription notifies only when a watched attribute's value changes, so subscriptions watch `dateObserved`, not `precipitation`, which is often 0.

## Path to production

- **Real sensors:** meters and weather stations would connect through FIWARE IoT Agents (MQTT/HTTP) instead of the simulator.
- **History:** add Mintaka or QuantumLeap for the NGSI-LD temporal API.
- **Security:** put Keyrock/Wilma (or Azure API Management) in front of the broker.
- **Fabric:** sync the broker to Fabric through a subscription-fed pipeline. Public SYKE catchments and Ruokavirasto parcels already have the matching entity shapes.
