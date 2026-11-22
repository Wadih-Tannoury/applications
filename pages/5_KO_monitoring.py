#!/usr/bin/env python3
"""
KO monitoring console.

The shipments stuck at the warehouse without documents: what the last upload
found, whether the KO has since been fixed, when the warehouse last tried to
print, and why it failed.

Unlike the other pages this one is mostly a window. It queues a pair of files —
the pending-monitoring CSV and the IMKO check workbook — into
`ko_monitoring/input/` of the operational_tools repository in a single commit,
and the KO monitoring workflow there does the work: it joins the two, calls the
shipment-documents API once per shipment, reads the warehouse's own attempts
out of BigQuery, and writes
`tlg-business-intelligence-prd.til.ko_operational_table`. That table is what
the Dashboard tab reads.

**One commit, two files.** The other consoles queue one small CSV through the
contents endpoint. The workbook is several megabytes, which that endpoint will
not take, and the two have to arrive together or the workflow starts on half a
batch — so this page commits through the git data API instead. See
`shared/ko_monitoring.py`.

**The table is the memory.** A shipment uploaded again is updated, never
duplicated. A shipment an upload no longer carries is marked `ko_resolved`
and drops off the working list without being deleted. `ko_fix_date`, once set,
is never overwritten. The GXO resolved filter opens on *Open only*, which is
the working list.

With no `[bigquery]` section in the secrets the page falls back to
`ko_monitoring/output/latest.csv` in the repository — the last run only, no
resolved shipments, no history — and says so under the heading.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.console import GitHub, settings, start_page  # noqa: E402
from shared import ko_monitoring as ko  # noqa: E402
from shared import ko_table  # noqa: E402

APP_KEY = "ko_monitoring"
LABEL = "KO monitoring"

# The columns that were asked for, in the order they were asked for.
HEADLINE_COLUMNS = [
    "shipment_order_number",
    "oerodp",
    "ub_imko",
    "logistic_order_number",
    "oms_shipment_number",
    "order_submit_date",
    "shipment_creation_date",
    "action",
    "fulfillment_status",
    "location_provider",
    "ko_fixed",
    "ko_fix_date",
    "last_tried_by_gxo_date",
    "tried_by_gxo_after_the_fix",
    "ko_reason",
]

# The evidence behind each ko_fix_date, shown only when it is asked for.
EVIDENCE_COLUMNS = [
    "ko_resolved", "ko_resolved_date", "gxo_result_code", "source", "brand",
    "eligible_for_package_closure",
    "label_shplbl_populated", "label_shplbl_format",
    "label_retlbl_present", "label_retlbl_populated", "label_retlbl_format",
    "shipping_tracking_number", "return_tracking_number",
    "doc_inv_populated", "doc_rpi_populated",
    "api_call_date", "batch", "last_run", "first_seen", "last_updated",
]

RUN_ICONS = {
    "success": "🟢", "failure": "🔴", "cancelled": "⚪", "skipped": "⚪",
    "in_progress": "🟡", "queued": "🟡", "requested": "🟡", "waiting": "🟡",
}


# ---------------------------------------------------------------------------
# Reading what the workflow produced
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def load_dataset(repo: str, branch: str, path: str, use_table: bool) -> Dict[str, Any]:
    """
    What the dashboard shows, from the table when there is one.

    Cached for a minute so moving a filter does not re-read; Refresh clears it,
    and so does queueing a run.
    """
    if use_table:
        read = ko_table.fetch()
        if read.ok:
            return {"frame": read.frame, "meta": {}, "error": "", "origin": "table",
                    "read_at": read.read_at, "missing_columns": read.missing_columns}
        table_error = read.error
    else:
        table_error = ""

    github = GitHub(settings(APP_KEY, ko.DEFAULT_FOLDERS))
    raw, error = ko.read_bytes(github, f"{path}/latest.csv")
    if error == "404":
        return {"frame": None, "meta": {}, "origin": "none", "error": (
            (table_error + " " if table_error else "")
            + "No data yet. Upload a pair of files on the **New run** tab; the "
              "workflow writes the table and `output/latest.csv` when it finishes."
        )}
    if error:
        return {"frame": None, "meta": {}, "origin": "none",
                "error": f"The dataset could not be read: {error}"}
    try:
        frame = ko.decode_csv(raw)
    except Exception as exc:  # noqa: BLE001
        return {"frame": None, "meta": {}, "origin": "none",
                "error": f"The dataset could not be parsed: {exc}"}

    # The CSV is one run, so it knows nothing about resolved shipments: the
    # columns are added empty rather than left out, so the page is the same
    # page whichever source it drew from.
    for column in ("ko_resolved", "ko_resolved_date"):
        if column not in frame.columns:
            frame[column] = ""

    meta: Dict[str, Any] = {}
    raw_meta, meta_error = ko.read_bytes(github, f"{path}/latest.json")
    if not meta_error:
        try:
            meta = json.loads(raw_meta.decode("utf-8"))
        except ValueError:
            meta = {}
    return {"frame": frame, "meta": meta, "origin": "csv", "error": "",
            "table_error": table_error}


def options(frame: pd.DataFrame, column: str) -> List[str]:
    if column not in frame.columns:
        return []
    return sorted({str(value).strip() for value in frame[column] if str(value).strip()})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def tab_dashboard(config) -> None:
    left, right = st.columns([5, 1])
    left.markdown("#### The shipments")
    if right.button("Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    loaded = load_dataset(config.repo, config.branch, ko.output_dir(), ko_table.configured())
    if loaded["error"]:
        st.info(loaded["error"])
        return

    frame: pd.DataFrame = loaded["frame"]
    meta: Dict[str, Any] = loaded["meta"]

    # The dashboard is the GXO working list: shipments still open, in a
    # warehouse this console can do something about. A shipment is GXO's when
    # it came from a GXO side of the upload or when the monitoring table says
    # its location provider is GXO — the second is what the shipments taken
    # from that table alone are recognised by, since their source is
    # MONITORING rather than a side of an upload.
    everything = frame
    gxo = pd.Series(False, index=frame.index)
    if "source" in frame.columns:
        gxo |= frame["source"].str.upper().str.contains("GXO", na=False)
    if "location_provider" in frame.columns:
        gxo |= frame["location_provider"].str.strip().str.upper().eq("GXO")
    if "source" in frame.columns or "location_provider" in frame.columns:
        frame = frame[gxo]
    # Resolved shipments are archived out of the table by the workflow, so
    # this normally takes nothing out; it is here for a row still waiting to
    # be archived after a run that could not finish clearing them.
    if "ko_resolved" in frame.columns:
        frame = frame[~frame["ko_resolved"].str.strip().str.lower().isin(
            ("yes", "true", "1"))]
    frame = frame.reset_index(drop=True)

    if loaded.get("origin") == "table":
        st.caption(
            f"{ko_table.FQN} · {len(frame)} shipment(s) ever seen · read "
            f"{ko.show_time(loaded['read_at'].isoformat())} UTC"
        )
        if loaded.get("missing_columns"):
            st.caption(
                "The table is missing "
                + ", ".join(f"`{column}`" for column in loaded["missing_columns"])
                + " — run the newest `sql/ko_operational_table.sql`."
            )
    else:
        if loaded.get("table_error"):
            st.caption(loaded["table_error"])
        if meta:
            note = (f"Last run {meta.get('run', '')} · built "
                    f"{ko.show_time(meta.get('built_at'))} UTC · "
                    f"{meta.get('rows', len(frame))} shipment(s)")
            if meta.get("dry_run"):
                note += " · dry run, nothing was called"
            st.caption(note + " · from the run CSV, not the table")

    def flag(column: str) -> pd.Series:
        if column not in frame:
            return pd.Series(False, index=frame.index)
        return frame[column].str.strip().ne("")

    # ko_fixed and ko_fix_date are written together and never disagree, but
    # reading both means an older row that predates the column still counts.
    fixed = flag("ko_fix_date") | (
        frame["ko_fixed"].str.strip().str.lower().isin(("yes", "true", "1"))
        if "ko_fixed" in frame else pd.Series(False, index=frame.index)
    )
    tried = flag("last_tried_by_gxo_date")
    tried_since = (
        frame["tried_by_gxo_after_the_fix"].str.strip().str.lower().isin(("yes", "true", "1"))
        if "tried_by_gxo_after_the_fix" in frame else pd.Series(False, index=frame.index)
    )
    resolved = (
        frame["ko_resolved"].str.strip().str.lower().isin(("yes", "true", "1"))
        if "ko_resolved" in frame else pd.Series(False, index=frame.index)
    )
    open_rows = ~resolved

    columns = st.columns(6)
    columns[0].metric("Open", int(open_rows.sum()))
    columns[1].metric("Fixed", int((fixed & open_rows).sum()))
    columns[2].metric("Still KO", int(((~fixed) & open_rows).sum()))
    columns[3].metric("Tried by GXO", int((tried & open_rows).sum()))
    # Fixed on paper but untouched by the warehouse since is the group worth
    # chasing, so it gets a number of its own rather than a filter.
    columns[4].metric("Fixed, not retried", int((fixed & open_rows & ~tried_since).sum()))
    columns[5].metric("Hidden", len(everything) - len(frame),
                      help="Rows in the table that this page does not show: "
                           "resolved, or not GXO's.")

    with st.expander("Filters", expanded=True):
        row = st.columns(5)
        state = row[0].selectbox("KO state", ["Everything", "Still KO", "Fixed"])
        actions = row[1].multiselect("Action", options(frame, "action"))
        codes = row[2].multiselect("ub_imko", options(frame, "ub_imko"))
        sources = row[3].multiselect("Source", options(frame, "source"))
        providers = row[4].multiselect(
            "Location provider", options(frame, "location_provider"))
        row = st.columns([3, 1])
        needle = row[0].text_input(
            "Search", placeholder="shipment, OERODP, logistic order, tracking number…"
        )
        show_evidence = row[1].checkbox("Show the evidence columns", value=False)

    shown = frame.copy()
    if providers:
        shown = shown[shown["location_provider"].isin(providers)]
    if state == "Fixed":
        shown = shown[fixed.reindex(shown.index, fill_value=False)]
    elif state == "Still KO":
        shown = shown[~fixed.reindex(shown.index, fill_value=False)]
    if actions:
        shown = shown[shown["action"].isin(actions)]
    if codes:
        shown = shown[shown["ub_imko"].isin(codes)]
    if sources:
        shown = shown[shown["source"].isin(sources)]
    if needle.strip():
        text = needle.strip().upper()
        shown = shown[shown.apply(
            lambda record: any(text in str(value).upper() for value in record.values), axis=1
        )]

    wanted = [column for column in HEADLINE_COLUMNS if column in shown.columns]
    if show_evidence:
        wanted += [column for column in EVIDENCE_COLUMNS if column in shown.columns]

    st.caption(f"{len(shown)} of {len(frame)} shipment(s)")
    st.dataframe(shown[wanted], use_container_width=True, hide_index=True, height=520)
    st.download_button(
        "Download what is on screen",
        shown[wanted].to_csv(index=False).encode("utf-8"),
        file_name=f"ko_monitoring-{ko.stamp()}.csv",
        mime="text/csv",
    )

    if len(shown):
        left, right = st.columns(2)
        still = shown[~fixed.reindex(shown.index, fill_value=False)]
        with left:
            st.markdown("###### Still KO, by action")
            breakdown(still, "action", "Action")
        with right:
            st.markdown("###### Why a shipment is still KO")
            breakdown(still, "ko_reason", "Reason")


def breakdown(frame: pd.DataFrame, column: str, heading: str, limit: int = 12) -> None:
    """
    A count per value, with a bar, as a native table.

    Deliberately not st.bar_chart: that imports altair, and altair 5.5 does not
    import at all on the Python 3.14 that Streamlit Cloud now runs. A dataframe
    with a ProgressColumn needs nothing but Streamlit itself, reads as well at
    this size, and the numbers are legible rather than inferred from a bar.
    """
    if not len(frame) or column not in frame.columns:
        st.caption("Nothing is still KO in this selection.")
        return
    counts = frame[column].replace("", "—").value_counts().head(limit)
    table = pd.DataFrame({heading: counts.index, "Shipments": counts.values})
    st.dataframe(
        table, hide_index=True, use_container_width=True,
        column_config={
            "Shipments": st.column_config.ProgressColumn(
                "Shipments", format="%d", min_value=0,
                max_value=int(counts.max()) if len(counts) else 1,
            )
        },
    )


# ---------------------------------------------------------------------------
# New run
# ---------------------------------------------------------------------------

def tab_new_run(config, github: GitHub, username: str) -> None:
    st.markdown("#### Start a run")
    st.caption(
        "Both files go into one commit, so the workflow starts once with the pair "
        "already complete. It then checks every shipment against the documents API, "
        "which takes a few minutes for a few hundred rows."
    )

    left, right = st.columns(2)
    csv_file = left.file_uploader(
        "The pending-monitoring CSV", type=["csv", "txt"],
        help="Every row is used; nothing is filtered out of this file. The detail "
             "export is the one to use — it carries Logistic order, OMS shipment # "
             "and Shipment created (effective).",
    )
    xlsx_file = right.file_uploader(
        "The check workbook", type=["xlsx", "xlsm"],
        help="Only the `imko` sheet is read, and only the rows whose ub_imko is "
             "IMKO 001–026 or IMKO 100–199.",
    )

    if csv_file is not None:
        st.caption(f"CSV: **{csv_file.name}** · {csv_file.size / 1024:.0f} KB")
    if xlsx_file is not None:
        st.caption(f"Workbook: **{xlsx_file.name}** · {xlsx_file.size / 1024 / 1024:.1f} MB")

    ready = csv_file is not None and xlsx_file is not None
    if not ready:
        st.info("Both files are needed: the workflow will not start on one of them.")

    if st.button("Queue the run", type="primary", disabled=not ready, use_container_width=True):
        batch = ko.stamp()
        user = "".join(character if character.isalnum() else "-" for character in username)[:40] or "user"
        suffix = ".xlsm" if xlsx_file.name.lower().endswith(".xlsm") else ".xlsx"
        files = [
            (f"{config.input_dir}/{batch}__{user}__pending.csv", csv_file.getvalue()),
            (f"{config.input_dir}/{batch}__{user}__check{suffix}", xlsx_file.getvalue()),
        ]
        with st.spinner("Committing both files…"):
            sha, error = ko.commit_files(
                github, files, f"KO monitoring batch {batch} from {username}"
            )
        if error:
            st.error(f"The files could not be committed: {error}")
            return
        st.cache_data.clear()
        st.success(
            f"Batch `{batch}` queued in `{config.input_dir}/` as commit `{sha[:7]}`. "
            "The workflow starts within a few seconds — watch it on the **Runs** tab, "
            "then come back to the Dashboard."
        )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def tab_runs(config, github: GitHub) -> None:
    left, right = st.columns([5, 1])
    left.markdown("#### The workflow")
    if right.button("Refresh ", use_container_width=True):
        st.rerun()

    found, error = ko.runs(github, limit=10)
    if error:
        st.warning(error)
    elif not found:
        st.info("The workflow has not run yet.")
    else:
        for run in found:
            state = run.get("conclusion") or run.get("status") or ""
            st.markdown(
                f"{RUN_ICONS.get(state, '⚪')} **{state or 'unknown'}** · "
                f"{ko.show_time(run.get('run_started_at'))} UTC · "
                f"{run.get('event', '')} · "
                f"[#{run.get('run_number')}]({run.get('html_url')})"
            )

    st.divider()
    st.markdown("##### Folders")
    columns = st.columns(4)
    folders = [
        ("Waiting", config.input_dir), ("Output", ko.output_dir()),
        ("Archive", config.archive_dir), ("Errors", config.error_dir),
    ]
    for column, (label, path) in zip(columns, folders):
        entries, folder_error = github.list_dir(path)
        files = [entry for entry in entries if not entry.get("name", "").startswith(".")]
        with column:
            st.metric(label, "—" if folder_error else len(files))
            st.caption(f"`{path}/`")
            if folder_error:
                st.caption(folder_error)
            else:
                for entry in sorted(files, key=lambda item: item["name"], reverse=True)[:6]:
                    st.caption(entry["name"])

    st.divider()
    st.caption(
        "A run can also be started by hand without uploading anything — useful to "
        "re-check whatever is already in the queue."
    )
    if st.button("Run the workflow now"):
        error = ko.dispatch(github)
        if error:
            st.error(error)
        else:
            st.success("Started. It appears in the list above within a few seconds.")


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def main() -> None:
    config = settings(APP_KEY, ko.DEFAULT_FOLDERS)
    username = start_page(
        LABEL, config,
        caption=f"{config.repo} · branch {config.branch} · {ko.output_dir()}/",
        app_key=APP_KEY, label=LABEL,
    )
    github = GitHub(config)

    dashboard, new_run, runs_tab, log = st.tabs(["Dashboard", "New run", "Runs", "Log"])
    with dashboard:
        tab_dashboard(config)
    with new_run:
        tab_new_run(config, github, username)
    with runs_tab:
        tab_runs(config, github)
    with log:
        from shared.console import tab_logs
        tab_logs(github, config)


# This file is a page of streamlit_app.py, so it runs on import by st.navigation.
main()
