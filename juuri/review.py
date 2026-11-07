"""Expert review: reviewer decisions on proposed corrections.

Decisions are stored in ``review/decisions.csv`` (plain CSV, so it versions cleanly in git and maps
onto a Fabric Delta table / Power Apps approval list in production). One row per decision:

    record_key, field, decision (approved | rejected), reviewer, comment, timestamp

Policy for issues nobody has reviewed yet:
* ``provisional`` (default) - apply proposals with confidence >= AUTO_APPLY_MIN as *provisional*
  corrections. Results are marked DRAFT until every applied correction is approved.
* ``strict`` - apply approved corrections only.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from . import config as C

AUTO_APPLY_MIN = 0.7
DECISION_COLS = ["record_key", "field", "decision", "reviewer", "comment", "timestamp"]


def load_decisions() -> pd.DataFrame:
    if C.DECISIONS_FILE.exists():
        d = pd.read_csv(C.DECISIONS_FILE, dtype=str)
        return d.drop_duplicates(["record_key", "field"], keep="last")
    return pd.DataFrame(columns=DECISION_COLS)


def save_decision(record_key: str, field: str, decision: str, reviewer: str, comment: str = "") -> None:
    assert decision in ("approved", "rejected")
    C.REVIEW_DIR.mkdir(exist_ok=True)
    row = pd.DataFrame([dict(record_key=record_key, field=field, decision=decision, reviewer=reviewer,
                             comment=comment, timestamp=dt.datetime.now().isoformat(timespec="seconds"))])
    row.to_csv(C.DECISIONS_FILE, mode="a", header=not C.DECISIONS_FILE.exists(), index=False)


def save_decisions(rows: list[dict]) -> None:
    for r in rows:
        save_decision(**r)


def resolve(issues: pd.DataFrame, policy: str = "provisional") -> pd.DataFrame:
    """Attach review status and whether each proposal is applied in this run."""
    d = load_decisions()
    x = issues.merge(d[["record_key", "field", "decision", "reviewer", "timestamp"]], on=["record_key", "field"], how="left")
    x["status"] = x.decision.fillna("pending")
    auto = (x.status == "pending") & (x.confidence >= AUTO_APPLY_MIN) & (policy == "provisional")
    x["applied"] = (x.status == "approved") | auto
    x.loc[x.action == "flag", "applied"] = False
    x["applied_as"] = x.status.where(x.status != "pending", "provisional").where(x.applied, "not applied")
    return x.drop(columns=["decision"])
