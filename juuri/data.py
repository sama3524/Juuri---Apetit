"""Bronze layer: load the raw package exactly as delivered, and attach stable record keys."""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

from . import config as C

C.use_reference_tools()
from ccn_tools import io as ccn_io  # noqa: E402


@lru_cache(maxsize=1)
def raw() -> dict:
    """All raw tables (dict of DataFrames). Cached; treat as read-only."""
    C.ensure_package()
    r = ccn_io.load_raw(C.PACKAGE_ROOT)
    d = C.DATA
    r["goo_registry"] = pd.read_csv(d / "02_raw_feeds" / "goo_registry_statement.csv")
    r["capex"] = pd.read_csv(d / "02_raw_feeds" / "capex_projects.csv")
    r["foodhills"] = pd.read_csv(d / "03_farming" / "foodhills_odlingsdata_se.csv", sep=";", decimal=",")
    r["current_calc"] = pd.read_csv(d / "03_farming" / "current_calculator_footprints.csv")
    r["catchments"] = pd.read_csv(d / "06_nature" / "catchments.csv")
    r["catchment_loads"] = pd.read_csv(d / "06_nature" / "monitored_catchment_loads.csv")
    r["origin_nature"] = pd.read_csv(d / "06_nature" / "origin_state_of_nature_indicative.csv")
    r["scenarios"] = pd.read_csv(d / "05_scenarios" / "transition_scenarios.csv")
    r["levers"] = pd.read_csv(d / "05_scenarios" / "decarbonisation_levers.csv")
    r["events"] = pd.read_csv(d / "05_scenarios" / "structural_events.csv")
    r["prices"] = pd.read_csv(d / "05_scenarios" / "contract_prices_and_replacement_premiums.csv")
    r["published"] = pd.read_csv(d / "05_scenarios" / "published_inventory_history.csv")
    r["draft_report"] = pd.read_csv(d / "05_scenarios" / "draft_report_summary_2025.csv")
    r["climate_cells"] = pd.read_csv(d / "01_master" / "climate_grid_cells.csv")
    r["water"] = pd.read_csv(d / "02_raw_feeds" / "water_balance.csv")
    r["source_registry"] = pd.read_csv(d / "07_frameworks" / "source_registry.csv")
    add_record_keys(r)
    return r


def add_record_keys(r: dict) -> None:
    """Record keys follow docs/TASKS_AND_SCORING.md so every number can be traced to a source row."""
    e = r["erp"]
    e["record_key"] = "erp:" + e.doc_no.astype(str) + ":" + e.line_no.astype(str)
    m = r["energy_meter"]
    m["record_key"] = "energy:" + m.site_id + ":" + m.year.astype(str) + "-" + m.month.map("{:02d}".format) + ":" + m.carrier
    r["fuel_deliveries"]["record_key"] = "fuel:" + r["fuel_deliveries"].delivery_id
    g = r["goo"]
    g["record_key"] = "goo:" + g.site_id + ":" + g.year.astype(str)
    r["refrigerants"]["record_key"] = "refrigerant:" + r["refrigerants"].event_id
    r["waste"]["record_key"] = "waste:" + r["waste"].transfer_note
    r["travel"]["record_key"] = "travel:" + r["travel"].booking_id
    r["commuting"]["record_key"] = "commute:" + r["commuting"].response_id
    r["farm_survey"]["record_key"] = "farm_survey:" + r["farm_survey"].survey_id
    r["farm_registry"]["record_key"] = "farm:" + r["farm_registry"].farm_id
    p = r["pcfs"]
    p["record_key"] = "pcf:" + p.supplier_id + ":" + p.material_id + ":" + p.year.astype(str)
    dr = r["draft_report"]
    dr["record_key"] = "report:" + dr.year.astype(str) + ":" + dr.line
    s = r["shipments"]
    s["ref_doc"] = s.ref_doc.astype(str)
    s["record_key"] = "ship:" + s.ref_doc + ":" + s.ef_id


SOURCE_FILES = {
    "erp_purchase_lines": "data/02_raw_feeds/erp_purchase_lines_2021_2025.csv",
    "energy_meter_readings": "data/02_raw_feeds/energy_meter_readings.csv",
    "fuel_deliveries": "data/02_raw_feeds/fuel_deliveries.csv",
    "goo_cancellations": "data/02_raw_feeds/goo_cancellations.csv",
    "refrigerant_service_log": "data/02_raw_feeds/refrigerant_service_log.csv",
    "waste_transfer_notes": "data/02_raw_feeds/waste_transfer_notes.csv",
    "travel_bookings": "data/02_raw_feeds/travel_bookings.csv",
    "commuting_survey": "data/02_raw_feeds/commuting_survey_2025.csv",
    "shipments": "data/02_raw_feeds/shipments.csv",
    "farm_survey": "data/03_farming/farm_survey_fi.csv",
    "farm_registry": "data/01_master/farm_registry.csv",
    "supplier_pcfs": "data/02_raw_feeds/supplier_pcfs/",
    "draft_report_summary": "data/05_scenarios/draft_report_summary_2025.csv",
}
