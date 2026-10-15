"""Transplant + 14-day feedback page.

* due date = transplant date + 14 days (computed by the service, not here),
* states: pending / due / overdue / completed,
* duplicate submissions for one transplant are blocked,
* corrections are possible only through an explicit form that requires a
  reason and records an audit entry (original values preserved),
* growth is shown only when BOTH heights exist - otherwise it stays empty.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from plantreadiness import config
from plantreadiness import services as svc
from webapp import components as ui


def render(repo, models_dir) -> None:
    st.title(":material/potted_plant: Transplant & 14-day feedback")
    st.caption(f"Feedback becomes due **{config.FEEDBACK_WINDOW_DAYS} days** "
               "after the transplant date.")

    try:
        plants = repo.list_plants()
        pending = repo.pending_feedback()
        history = repo.all_feedback()
    except Exception as exc:
        ui.error_box(exc)
        return

    tab_record, tab_due, tab_history = st.tabs(
        ["Record transplant", "Pending / due feedback", "History & corrections"])

    with tab_record:
        _record_tab(repo, plants)

    with tab_due:
        _due_tab(repo, pending)

    with tab_history:
        _history_tab(repo, history)


def _record_tab(repo, plants) -> None:
    if not plants:
        st.info("Register a plant first.")
        return
    labels = {f"#{p['plant_id']} {p['species']}"
              f"{' / ' + p['variety'] if p['variety'] else ''}"
              f" [{p['data_source']}]": p["plant_id"] for p in plants}
    with st.form("record_transplant", clear_on_submit=True):
        chosen = st.selectbox("Plant", list(labels.keys()))
        plant_id = labels[chosen]
        t_date = st.date_input("Transplant date", value=date.today())
        height = st.number_input("Height at transplant (cm)", min_value=0.0,
                                 value=0.0, step=0.1)
        st.caption(f"Feedback will be due on "
                   f"**(transplant + {config.FEEDBACK_WINDOW_DAYS})** - "
                   "computed automatically.")
        ok = st.form_submit_button("Record transplant", type="primary")
    if not ok:
        return
    try:
        rec = svc.record_transplant(
            repo, plant_id=plant_id, transplant_date=t_date.isoformat(),
            height_at_transplant_cm=float(height) if height > 0 else None)
    except Exception as exc:
        ui.error_box(exc)
        return
    st.success(f"Transplant recorded. Feedback **#{rec['feedback_id']}** is "
               f"due on **{rec['feedback_due_date']}**.")


def _due_tab(repo, pending) -> None:
    if not pending:
        st.success("No pending 14-day feedback.")
        return
    rows = []
    for r in pending:
        state = svc.feedback_state(r)
        rows.append({"Feedback #": r["feedback_id"],
                     "Plant": f"#{r['plant_id']} {r['species']}"
                              f"{'/' + r['variety'] if r.get('variety') else ''}",
                     "Transplant": r.get("transplant_date"),
                     "Due": r["feedback_due_date"],
                     "Status": state["status"],
                     "Overdue (days)": state["days_overdue"] or "",
                     "Source": r.get("data_source")})
    st.dataframe(rows, hide_index=True, width="stretch")

    overdue = [r for r in rows if r["Status"] == "overdue"]
    if overdue:
        st.warning(f"{len(overdue)} follow-up(s) overdue.")
    due_now = [r for r in rows if r["Status"] == "due"]
    if due_now:
        st.info(f"{len(due_now)} follow-up(s) due today.")

    st.subheader("Submit an outcome")
    # options come from the source rows (the display dicts use pretty
    # column names like "Feedback #", not the raw keys)
    options = {f"#{r['feedback_id']} - plant #{r['plant_id']} "
               f"(due {r['feedback_due_date']}, "
               f"{svc.feedback_state(r)['status']})": r["feedback_id"]
               for r in pending}
    chosen = st.selectbox("Feedback record", list(options.keys()))
    feedback_id = options[chosen]

    with st.form("submit_feedback", clear_on_submit=True):
        sub_date = st.date_input("Submission date", value=date.today())
        survived = st.radio("Survived after 14 days?", ["Yes", "No"],
                            horizontal=True) == "Yes"
        col1, col2 = st.columns(2)
        height_after = col1.number_input("Height after 14 days (cm)",
                                         min_value=0.0, value=0.0, step=0.1)
        condition = col2.selectbox("Visible condition",
                                   ["(not recorded)"] +
                                   list(config.VISIBLE_CONDITIONS))
        notes = st.text_area("Notes")
        st.caption("Growth is computed as *after − at transplant* and stays "
                   "empty when either height is missing - nothing is "
                   "invented.")
        ok = st.form_submit_button("Submit outcome", type="primary")
    if not ok:
        return
    try:
        result = svc.submit_feedback(
            repo, feedback_id=feedback_id,
            feedback_submitted_date=sub_date.isoformat(),
            survived_after_14d=survived,
            height_after_14d_cm=float(height_after) if height_after > 0
            else None,
            visible_condition=(None if condition == "(not recorded)"
                               else condition),
            user_notes=notes or None)
    except Exception as exc:
        ui.error_box(exc)
        return
    st.success(f"Feedback #{feedback_id} submitted.")
    if result.get("growth_after_14d_cm") is None:
        st.caption("Growth after 14 days: **not measurable** (left empty).")
    else:
        st.metric("Growth after 14 days",
                  f"{result['growth_after_14d_cm']} cm")


def _history_tab(repo, history) -> None:
    if not history:
        st.caption("No transplant/feedback records yet.")
        return
    rows = []
    for r in history:
        state = svc.feedback_state(r)
        rows.append({"Feedback #": r["feedback_id"],
                     "Plant": f"#{r['plant_id']}",
                     "Species": r.get("species"),
                     "Transplant": r.get("transplant_date"),
                     "Due": r["feedback_due_date"],
                     "Submitted": r.get("feedback_submitted_date"),
                     "Status": state["status"],
                     "Survived": ("" if r.get("survived_after_14d") is None
                                  else bool(r.get("survived_after_14d"))),
                     "Height after": r.get("height_after_14d_cm"),
                     "Growth": r.get("growth_after_14d_cm"),
                     "Condition": r.get("visible_condition"),
                     "Source": r.get("data_source")})
    st.dataframe(rows, hide_index=True, width="stretch")

    # ---- explicit correction (audited) ------------------------------------ #
    submitted = [r for r in history if r.get("feedback_submitted_date")]
    if not submitted:
        return
    st.subheader("Correct a submitted outcome")
    st.caption("Corrections are explicit and audited: the original values "
               "are kept in history and a reason is mandatory.")
    opts = {f"#{r['feedback_id']} (submitted "
            f"{r['feedback_submitted_date']})": r["feedback_id"]
            for r in submitted}
    chosen = st.selectbox("Record to correct", list(opts.keys()),
                          key="correct_choice")
    feedback_id = opts[chosen]
    revisions = []
    try:
        revisions = repo.feedback_revisions(feedback_id)
    except Exception:
        revisions = []

    with st.form("correct_feedback"):
        reason = st.text_input("Reason for correction *",
                               help="Mandatory - stored in the audit trail.")
        col1, col2 = st.columns(2)
        survived_in = col2.radio("Survived?", ["(keep current)", "Yes", "No"],
                                 horizontal=True)
        height_in = col1.number_input("Height after 14 days (cm)",
                                      min_value=0.0, value=0.0, step=0.1)
        condition = col2.selectbox("Condition",
                                   ["(keep current)"] +
                                   list(config.VISIBLE_CONDITIONS))
        notes_in = st.text_area("Notes", value="")
        ok = st.form_submit_button("Apply correction", type="primary")
    if not ok:
        return
    if not reason.strip():
        st.error("A correction must state a reason - audit history is "
                 "mandatory.")
        return
    try:
        result = svc.correct_feedback(
            repo, feedback_id=feedback_id,
            survived_after_14d=(None if survived_in == "(keep current)"
                                else survived_in == "Yes"),
            height_after_14d_cm=float(height_in) if height_in > 0 else None,
            visible_condition=(None if condition == "(keep current)"
                               else condition),
            user_notes=notes_in or None,
            reason=reason.strip())
    except Exception as exc:
        ui.error_box(exc)
        return
    st.success(f"Feedback #{feedback_id} corrected. Growth now: "
               f"{result.get('growth_after_14d_cm')}")
    try:
        revisions = repo.feedback_revisions(feedback_id)
    except Exception:
        revisions = []
    if revisions:
        with st.expander(f"Audit history ({len(revisions)} correction(s))",
                         expanded=True):
            st.dataframe(
                [{"When": v.get("corrected_at"),
                  "Reason": v.get("reason"),
                  "Original values": v.get("previous_values")}
                 for v in revisions],
                hide_index=True, width="stretch")
