"""Species & data management page.

* evidence counts per species (real vs synthetic clearly separated),
* CSV import (standard growth measurements),
* ESP32-CAM / OpenCV hardware measurement import (plant_daily_measurements.csv),
* CSV export for backup (including readiness events),
* plant search/filter.

Imports are append-only and idempotent (duplicates are skipped, never
overwritten) and always land in ``data_source='real'`` - this page imports
measurements, never readiness labels.
"""

from __future__ import annotations

import io
from pathlib import Path

import streamlit as st

from plantreadiness import config
from plantreadiness import services as svc
from webapp import components as ui


def render(repo, models_dir) -> None:
    st.title(":material/science: Species & data management")

    tab_species, tab_import, tab_camera, tab_export = st.tabs(
        [
            "Evidence by species",
            "Import standard CSV",
            "Import camera CSV (ESP32-CAM)",
            "Export data",
        ]
    )

    with tab_species:
        _species_tab(repo, models_dir)

    with tab_import:
        _import_tab(repo)

    with tab_camera:
        _camera_import_tab(repo)

    with tab_export:
        _export_tab(repo)


def _species_tab(repo, models_dir) -> None:
    try:
        by_species = repo.readiness_counts_by_species()
        all_plants = repo.list_plants()
    except Exception as exc:
        ui.error_box(exc)
        return
    if not by_species:
        st.info("No plants registered yet.")
        return

    species_names = sorted({r["species"] for r in by_species})
    selected = st.multiselect("Filter species", species_names, default=species_names)
    st.subheader("Evidence counts")
    rows = []
    for r in by_species:
        if r["species"] not in selected:
            continue
        meets = (
            r["data_source"] == "real"
            and r["n_events"] >= config.MIN_READINESS_EVENTS
            and r["n_plants_with_event"] >= config.MIN_INDEPENDENT_PLANTS
        )
        if r["data_source"] != "real":
            usable = "no - synthetic only"
        else:
            usable = (
                "yes"
                if meets
                else f"no - needs {config.MIN_READINESS_EVENTS} events / "
                f"{config.MIN_INDEPENDENT_PLANTS} plants"
            )
        rows.append(
            {
                "Species": r["species"],
                "Source": r["data_source"],
                "Plants": r["n_plants"],
                "Plants with readiness event": r["n_plants_with_event"],
                "Readiness events": r["n_events"],
                "Usable for training": usable,
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    st.caption(
        f"Minimum to train/validate a **real** model: "
        f"{config.MIN_INDEPENDENT_PLANTS} independent plants **and** "
        f"{config.MIN_READINESS_EVENTS} verified readiness events. "
        f"{config.UNAVAILABLE_MESSAGE} until then."
    )

    st.subheader("Species detail")
    species = st.selectbox("Species", species_names, key="species_detail")
    variety_opts = sorted(
        {p["variety"] for p in all_plants if p["species"] == species and p["variety"]}
    )
    variety = st.selectbox(
        "Variety", ["(any)"] + variety_opts, key="species_variety"
    )
    try:
        summary = repo.species_data_summary(
            species, None if variety == "(any)" else variety
        )
        plan = svc.species_model_plan(
            repo, species, None if variety == "(any)" else variety, models_dir
        )
    except Exception as exc:
        ui.error_box(exc)
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("Real plants", summary["plants"].get("real", 0))
    col2.metric("Synthetic plants", summary["plants"].get("synthetic", 0))
    col3.metric(
        "Real readiness events", summary["real_readiness_events"]["readiness_events"]
    )
    st.write(f"**Model selection:** Level {plan['level']} - {plan['message']}")
    if plan["model_name"]:
        st.caption(f"Artefact: `{plan['model_name']}`")

    # search/filter plants of this species
    st.subheader("Plants")
    search = st.text_input(
        "Search within species",
        placeholder="variety / ref / id",
        key="species_plant_search",
    )
    filtered = [p for p in all_plants if p["species"] == species]
    if search:
        needle = search.strip().lower()
        filtered = [
            p
            for p in filtered
            if needle
            in (
                (p.get("variety") or "") + " " + (p.get("external_ref") or "")
            ).lower()
            or needle in str(p["plant_id"])
        ]
    if filtered:
        st.dataframe(
            [
                {
                    "#": p["plant_id"],
                    "Variety": p["variety"],
                    "Started": p["date_started"],
                    "Ref": p["external_ref"],
                    "Cohort": p["cohort"],
                    "Source": p["data_source"],
                }
                for p in filtered
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No plants match.")


def _import_tab(repo) -> None:
    st.markdown(
        "Import growth **measurements** (columns "
        "`Date, Age_days, Plant_ID, Height_cm[, Growth_rate_cm_per_day]`).\n\n"
        "This never creates readiness labels - those only come from verified "
        "assessments. Rows already stored are **skipped**, never overwritten."
    )
    uploaded = st.file_uploader("CSV file", type="csv", key="std_csv_uploader")
    if uploaded is None:
        st.caption(
            "Upload a CSV to preview it. Nothing is written until you press Import."
        )
        return
    try:
        raw = uploaded.getvalue()
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        st.error("The file is not valid UTF-8 text.")
        return

    col1, col2 = st.columns(2)
    species = col1.text_input(
        "Species for imported plants",
        value=config.DEFAULT_SPECIES,
        key="std_csv_species",
    )
    variety = col2.text_input("Variety (optional)", key="std_csv_variety")

    try:
        preview = svc.preview_csv_import(csv_text)
    except Exception as exc:
        ui.error_box(exc)
        return

    st.subheader("Preview & validation")
    if preview["issues"]:
        st.warning(
            f"{len(preview['issues'])} row issue(s) - these rows will "
            "be skipped (no invented data):"
        )
        st.dataframe(preview["issues"], hide_index=True)
    else:
        st.success(f"All {preview['rows_total']} rows look valid.")
    st.caption(
        f"Rows: {preview['rows_total']} | plants in file: "
        f"{len(preview['plants_in_file'])} | new plants: "
        f"{len(preview['plants_new'])}"
    )
    if preview["rows_preview"]:
        st.dataframe(preview["rows_preview"], hide_index=True)

    if st.button("Import", type="primary", disabled=not preview["rows_total"]):
        try:
            report = svc.run_csv_import(
                repo,
                csv_text,
                species=species,
                variety=variety or None,
                dry_run=False,
            )
        except Exception as exc:
            ui.error_box(exc)
            return
        st.success(
            f"Imported {report['observations_inserted']} "
            f"observation(s); skipped "
            f"{report['observations_skipped_duplicate']} duplicate(s); "
            f"registered {len(report['preview']['plants_new'])} new plant(s)."
        )
        if report["issues"]:
            st.warning(f"{len(report['issues'])} row issue(s) skipped.")
        st.caption("Re-running the same file is safe: duplicates are skipped.")


def _camera_import_tab(repo) -> None:
    st.subheader("Ingest ESP32-CAM Daily Measurements")
    st.markdown(
        "Import daily measurements recorded by `plant_measurement.py` "
        "(`plant_daily_measurements.csv`)."
    )

    with st.expander("Verified column semantics & mapping rules", expanded=False):
        st.markdown(
            "- **`plant_id`**: Matched against existing plant `external_ref`.\n"
            "- **`average_height_cm`**: Plant height apex-to-stem-base stored as `height_cm`.\n"
            "- **`average_width_cm`**: **Stem width** (caliper) from OpenCV. Intentionally NOT mapped to canopy width.\n"
            "- **`average_visible_leaves`**: Stored as `visible_true_leaf_count`.\n"
            "- **Capture timestamp**: Stored as observation date.\n"
            "- **Duplicate protection**: Repeated uploads skip already stored date records."
        )

    col_btn, _ = st.columns([1, 2])
    local_default = Path("plant_daily_measurements.csv")
    has_local = local_default.exists()

    col1, col2 = st.columns(2)
    def_sp = col1.text_input(
        "Default species for new camera plants",
        value=config.DEFAULT_SPECIES,
        key="cam_sp",
    )
    def_var = col2.text_input(
        "Default variety (optional)", key="cam_var"
    )
    auto_reg = st.checkbox(
        "Auto-register unknown plant identifiers",
        value=True,
        help="Creates plant records if the plant_id in CSV does not exist yet.",
    )

    uploaded_cam = st.file_uploader(
        "Upload camera CSV file", type=["csv"], key="cam_csv_uploader"
    )

    sync_local = False
    if has_local:
        sync_local = st.button(
            "Sync from local plant_daily_measurements.csv", key="btn_sync_local_csv"
        )

    if uploaded_cam or sync_local:
        try:
            if uploaded_cam:
                raw_bytes = uploaded_cam.getvalue()
                buf = io.StringIO(raw_bytes.decode("utf-8-sig"))
                source_label = uploaded_cam.name
            else:
                buf = local_default
                source_label = str(local_default)

            report = svc.import_camera_measurements(
                repo,
                buf,
                default_species=def_sp.strip(),
                default_variety=def_var.strip() or None,
                auto_register=auto_reg,
            )
        except Exception as exc:
            ui.error_box(exc)
            return

        st.success(
            f"Processed `{source_label}`: "
            f"**{report['observations_inserted']}** observation(s) inserted, "
            f"**{report['observations_skipped_duplicate']}** duplicate(s) skipped, "
            f"**{report['plants_registered']}** new plant(s) auto-registered."
        )
        if report["unmatched_plants"]:
            st.warning(
                f"Unmatched plant IDs (skipped because auto-register is off): "
                f"{report['unmatched_plants']}"
            )
        if report["issues"]:
            with st.expander(f"Issues noted ({len(report['issues'])})"):
                st.dataframe(report["issues"], hide_index=True)


def _export_tab(repo) -> None:
    st.markdown("Download a CSV backup of the data this account can see.")
    for table, label in (
        ("plants", "Plants"),
        ("observations", "Observations"),
        ("feedback_14d", "14-day feedback"),
    ):
        try:
            data = svc.export_table_csv(repo, table)
        except Exception as exc:
            ui.error_box(exc)
            return
        n = len(data.splitlines()) - 1 if data else 0
        st.download_button(
            f"Download {label} CSV ({n} rows)",
            data=data or "",
            file_name=f"{table}.csv",
            mime="text/csv",
            disabled=(n == 0),
            key=f"dl_{table}",
        )
    st.caption(
        "Exports contain only this account's rows - Row Level Security applies to reads as well as writes."
    )
