"""Juuri command line.

    python -m juuri setup      # unzip the data package from Data/ into package/
    python -m juuri run        # bronze -> silver -> gold: writes every output and submission to outputs/
    python -m juuri score      # score the pipeline on the public dev answer keys (2023-2024 etc.)
    python -m juuri serve      # start the Juuri web product at http://localhost:8000
    python -m juuri fiware up|load|listen|simulate|demo|status|down   # FIWARE live context layer
    python -m juuri ingest <file-or-folder> --source "..."   # store a new data delivery (validated, append-only)
    python -m juuri deliveries | snapshots                   # list data deliveries / frozen snapshots
    python -m juuri freeze <label> [--year 2025] [--policy strict|provisional]   # freeze reported figures
    python -m juuri compare <label>                          # reported vs recalculated now (5 % threshold)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import warnings
import zipfile

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

from . import config as C  # noqa: E402


def setup():
    target = C.PACKAGE_ROOT.parent
    if C.DATA.exists():
        print(f"Package already at {C.PACKAGE_ROOT}")
        return
    if not C.PACKAGE_ZIP.exists():
        sys.exit(f"Put {C.PACKAGE_ZIP.name} in {C.PACKAGE_ZIP.parent} first.")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(C.PACKAGE_ZIP) as z:
        for m in z.namelist():   # refuse path traversal
            if m.startswith("/") or ".." in m.split("/"):
                sys.exit(f"Unsafe path in zip: {m}")
        z.extractall(target)
    print(f"Unzipped to {C.PACKAGE_ROOT}")


def _step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run():
    from . import climate as CL, copilot as CP, inventory as INV, nature as N, pathways as P, quality as Q
    out = C.OUTPUTS
    sub = out / "submissions"
    sub.mkdir(parents=True, exist_ok=True)
    _step("A1 data-quality agent")
    T = INV.canonical()
    iss = T["_issues"]
    iss.to_csv(out / "issues_all_years.csv", index=False)
    T["_audit"].to_csv(out / "audit_log.csv", index=False)
    Q.a1_submission(iss).to_csv(sub / "A1_issues_submission.csv", index=False)
    _step("A2 inventory + lineage")
    INV.all_years().to_csv(out / "inventory_2021_2025.csv", index=False)
    INV.a2_submission(T).to_csv(sub / "A2_inventory_2025_submission.csv", index=False)
    INV.line_items(T, 2025).to_csv(out / "inventory_line_items_2025.csv", index=False)
    INV.lineage(T, 2025).to_csv(out / "lineage_2025.csv", index=False)
    INV.flag_split(T, 2025).to_csv(out / "flag_split_2025.csv", index=False)
    _step("B pathways + MACC")
    b = P.base_inventory()
    P.base_year_options(b).to_csv(out / "B_base_year_options.csv", index=False)
    P.targets(b).to_csv(out / "B_targets.csv", index=False)
    P.lever_table(b).to_csv(out / "B_levers.csv", index=False)
    pd.concat([P.pathway(b, s) for s in ["net_zero_2050", "delayed_transition", "current_policies"]]).to_csv(out / "B_pathways.csv", index=False)
    P.macc(b).to_csv(out / "B_macc_2030.csv", index=False)
    _step("C climate risk (weather features, yield model)")
    CL.c1_predictions().to_csv(sub / "C1_yields_2027_2031_submission.csv", index=False)
    CL.c2_submission().to_csv(sub / "C2_climate_yield_risk_submission.csv", index=False)
    CL.c3_euro_risk().to_csv(out / "C3_euro_risk.csv", index=False)
    CL.backtest(2019).to_csv(out / "C_backtest_2020_2026.csv", index=False)
    _step("D nature")
    N.d1_submission().to_csv(sub / "D1_catchment_priority_submission.csv", index=False)
    N.d1_priority().to_csv(out / "D1_catchment_priority_detail.csv", index=False)
    N.parcel_hotspots(50).to_csv(out / "D1_parcel_hotspots.csv", index=False)
    N.d2_screening().to_csv(out / "D2_materiality_screening.csv", index=False)
    _step("E answers")
    CP.e_submission().to_csv(sub / "E_answers_submission.csv", index=False)
    status = iss.applied_as.value_counts().to_dict()
    print(json.dumps({"corrections": status, "outputs": str(out)}, indent=1))


def score():
    from . import inventory as INV, nature as N, quality as Q
    C.use_reference_tools()
    from ccn_tools import baselines as B, score as SC
    from ccn_tools import io as ccn_io
    T = INV.canonical()
    res = {}
    iss = T["_issues"]
    res["A1_dev_2023_2024"] = SC.score_a1(iss[iss.year.isin([2023, 2024])].drop_duplicates("record_key"),
                                          pd.read_csv(C.TASKS / "dev" / "A1_dev_issue_labels_2023_2024.csv"))["score"]
    for y in (2023, 2024):
        res[f"A2_dev_{y}"] = SC.score_a2(INV.summary(T, y), pd.read_csv(C.TASKS / "dev" / f"A2_dev_inventory_{y}.csv"))["score"]
    res["D1_dev (in-sample: the 6 dev catchments are the calibration set)"] = SC.score_d1(
        N.d1_submission(), pd.read_csv(C.TASKS / "dev" / "D1_dev_catchments_2025.csv"))["score"]
    s24 = INV.summary(T, 2024).set_index("category").tco2e
    e = pd.DataFrame(dict(question_id=["E90", "E91", "E92"], answer=[s24["scope1_total"], s24["scope2_market_total"],
                                                                     s24["cat01_purchased_goods_services"]]))
    res["E_dev_numeric"] = SC.score_e(e, pd.read_csv(C.TASKS / "dev" / "E_dev_answers.csv"))["score"]
    raw = ccn_io.load_raw(C.PACKAGE_ROOT)
    base = {"A1_dev_2023_2024": SC.score_a1(B.a1_rules(raw), pd.read_csv(C.TASKS / "dev" / "A1_dev_issue_labels_2023_2024.csv"))["score"]}
    for y in (2023, 2024):
        base[f"A2_dev_{y}"] = SC.score_a2(B.a2_naive(raw, y), pd.read_csv(C.TASKS / "dev" / f"A2_dev_inventory_{y}.csv"))["score"]
    print(json.dumps({"juuri": res, "package_baselines": base}, indent=1))
    C.OUTPUTS.mkdir(exist_ok=True)
    (C.OUTPUTS / "dev_scores.json").write_text(json.dumps({"juuri": res, "package_baselines": base}, indent=1))


def serve():
    import os
    import uvicorn
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PORT", 8000))
    print(f"Juuri running at http://localhost:{port}  (API docs: /api/docs)", flush=True)
    uvicorn.run("juuri.api:app", host="127.0.0.1", port=port, log_level="warning")


def store_cmd():
    from . import store
    store.main(sys.argv[1:])


def fiware():
    from . import fiware as FW
    FW.main(sys.argv[2:])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"setup": setup, "run": run, "score": score, "serve": serve, "fiware": fiware,
     **{k: store_cmd for k in ("ingest", "deliveries", "freeze", "snapshots", "compare")}}.get(cmd, lambda: sys.exit(__doc__))()
