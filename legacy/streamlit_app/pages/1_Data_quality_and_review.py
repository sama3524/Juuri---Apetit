"""Data-quality agent findings and the expert-review workflow."""
from common import T, csv_bytes, evidence, page, status_badge

import plotly.express as px
import streamlit as st

from juuri import review as RV

policy = page("Data quality & expert review", "🔎")
t = T(policy)
st.markdown(status_badge(t), unsafe_allow_html=True)
st.caption("The agent proposes; a person decides. Each proposal shows the rule that fired, the evidence and the value it would "
           "change. Approve or reject below: decisions go to `review/decisions.csv` and every page recalculates.")

iss = t["_issues"].copy()
c = st.columns(4)
years = sorted(iss.year.unique())
year = c[0].selectbox("Year", ["all"] + years[::-1], index=1 if 2025 in years else 0)
table = c[1].selectbox("Feed", ["all"] + sorted(iss.table.unique()))
itype = c[2].selectbox("Issue type", ["all"] + sorted(iss.issue_type.unique()))
status = c[3].selectbox("Review status", ["all", "pending", "approved", "rejected"])
x = iss
if year != "all":
    x = x[x.year == year]
if table != "all":
    x = x[x.table == table]
if itype != "all":
    x = x[x.issue_type == itype]
if status != "all":
    x = x[x.status == status]

l, r = st.columns([2, 1])
with r:
    g = x.groupby("issue_type").size().reset_index(name="n").sort_values("n")
    fig = px.bar(g, x="n", y="issue_type", orientation="h", height=420, color_discrete_sequence=["#2f6f4f"])
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_title=None, xaxis_title="issues")
    st.plotly_chart(fig)
with l:
    view = x[["issue_id", "record_key", "issue_type", "field", "old_value", "new_value", "confidence", "status", "applied_as",
              "rule", "explanation"]].copy()
    view.insert(0, "select", False)
    edited = st.data_editor(view, hide_index=True, height=420, width="stretch",
                            disabled=[c for c in view.columns if c != "select"],
                            column_config={"select": st.column_config.CheckboxColumn("✓"),
                                           "confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    chosen = edited[edited.select]

st.markdown(f"**{len(x):,} issues shown · {len(chosen)} selected**")
a, b2, c2, d = st.columns([1.2, 1, 1, 2])
reviewer = a.text_input("Reviewer", value=st.session_state.get("reviewer", ""), placeholder="name / initials")
comment = d.text_input("Comment (stored with the decision)")
st.session_state["reviewer"] = reviewer


def _decide(rows, decision):
    if not reviewer:
        st.warning("Enter the reviewer's name first: every decision is attributed.")
        return
    full = iss.set_index("issue_id").loc[rows.issue_id]
    RV.save_decisions([dict(record_key=k, field=f, decision=decision, reviewer=reviewer, comment=comment)
                       for k, f in zip(full.record_key, full.field)])
    st.success(f"{len(rows)} decisions recorded as {decision}.")
    st.rerun()


if b2.button("✅ Approve selected", disabled=chosen.empty, width="stretch"):
    _decide(chosen, "approved")
if c2.button("❌ Reject selected", disabled=chosen.empty, width="stretch"):
    _decide(chosen, "rejected")
with st.expander("Bulk-approve by rule (high confidence only)"):
    rule = st.selectbox("Rule", sorted(x.rule.unique()) if len(x) else [])
    thr = st.slider("Minimum confidence", 0.5, 1.0, 0.9, 0.05)
    bulk = x[(x.rule == rule) & (x.confidence >= thr) & (x.status == "pending")]
    st.write(f"{len(bulk)} pending proposals from **{rule}** at confidence ≥ {thr:.2f}")
    if st.button("Approve these", disabled=bulk.empty):
        _decide(bulk, "approved")

st.subheader("Audit log")
evidence("Gold numbers are reproducible from the raw (bronze) files plus this log: record_key, field, old → new value, rule, evidence, "
         "proposer, approver, status, timestamp. Fabric: store as a Delta table; Power Apps for approvals.")
audit = t["_audit"]
st.dataframe(audit, hide_index=True, width="stretch", height=300)
st.download_button("Download audit log (CSV)", csv_bytes(audit), "audit_log.csv", "text/csv")
st.download_button("Download issues (CSV)", csv_bytes(iss), "issues.csv", "text/csv")
