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
duplicated. `ko_fix_date`, once set, is never overwritten. A shipment whose KO
is resolved is moved to `til.ko_operational_history_table` and removed from the
operational table, so what this page reads is the working list by construction.

**Two views.** *GXO's list* — every shipment still open at GXO, whether the
warehouse has tried it or not — is what every login sees. *Troubleshooting* is
the table as it stands, nothing filtered and four more columns, and goes to
administrators and to any login named in `[ko_monitoring] full_view_users`.
Those users get a picker; a TLG login is not shown one.

The metrics count by `action_needed_from_gxo`, not by `ko_fixed`: what matters
is what somebody still has to go and do, and a shipment can be fixed and still
need its documents printed.

With no `[bigquery]` section in the secrets the page falls back to
`ko_monitoring/output/latest.csv` in the repository — the last run only, no
resolved shipments, no history — and says so under the heading.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.console import GitHub, is_admin, settings, start_page  # noqa: E402
from shared import ko_monitoring as ko  # noqa: E402
from shared import ko_table  # noqa: E402

APP_KEY = "ko_monitoring"
LABEL = "KO monitoring"

# The dashboard's own columns, in the order they were asked for. Both views
# show these; everything else in the table is behind "Show the evidence
# columns".
DASHBOARD_COLUMNS = [
    "shipment_order_number",
    "oerodp",
    "ub_imko",
    "brand",
    "channel",
    "order_submit_date",
    "shipment_creation_date",
    "fulfillment_status",
    "source",
    "gxo_result_code",
    "ko_fixed",
    "last_tried_by_gxo_date",
    "ko_fix_date",
    "tried_by_gxo_after_the_fix",
    "action_needed_from_gxo",
]

# The troubleshooting view's columns: the dashboard's, plus the four that say
# why a shipment is where it is. action and ko_reason are this workflow's own
# account of the last thing it did; blocking_reason is the monitoring table's;
# ko_source says which of them put the shipment in the table in the first
# place. They are the columns you want when a row looks wrong, and noise on a
# list meant for the warehouse — hence a view of its own.
TROUBLESHOOTING_COLUMNS = DASHBOARD_COLUMNS + [
    "action",
    "ko_reason",
    "blocking_reason",
    "ko_source",
]

# What the warehouse has been asked to do, exactly as the workflow writes it
# into action_needed_from_gxo. The metrics row counts by these rather than by
# ko_fixed: "fixed" is this workflow's verdict on the documents call, while the
# action is what somebody still has to go and do, and those are not the same
# question. A shipment can be fixed and still need its documents printed.
ACTION_PRINT = "To print shipping documents"
ACTION_CANCEL = "To cancel the shipment"

# A shipment blocked behind another one that is holding its stock. Nothing can
# be asked of the warehouse until that other shipment is dealt with, so these
# are kept off both dashboards. The reason names the shipment, so it is
# matched on its opening words rather than in full.
STOCK_RESERVED = "Stock still reserved to the cancelled shipment"

# Who sees the whole working list rather than GXO's. Anyone in
# [ko_monitoring] full_view_users, plus administrators. Everyone else gets the
# TLG view, which is the narrower of the two.
DEFAULT_FULL_VIEW_USERS = ("wadih",)

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
    "ko_resolved", "ko_resolved_date", "in_latest_gxo_input",
    "logistic_order_number", "oms_shipment_number", "action",
    "location_provider", "ko_reason", "blocking_reason",
    "eligible_for_package_closure",
    "shipping_tracking_number", "return_tracking_number",
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


def wanted_columns(frame: pd.DataFrame, view: str = "tlg") -> List[str]:
    """The view's columns that this frame actually has, in order."""
    columns = TROUBLESHOOTING_COLUMNS if view == "debug" else DASHBOARD_COLUMNS
    return [column for column in columns if column in frame.columns]


XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")


def excel_bytes(frame: pd.DataFrame,
                sheet: str = "KO monitoring") -> Tuple[bytes, str]:
    """
    What is on screen, as a .xlsx, and why not when it could not be built.

    Returns (bytes, reason). An empty reason means the workbook is good.

    pandas writes .xlsx through a third-party engine and installs none of
    them, so the usual cause of a CSV where a workbook was expected is a
    deployment that has not picked up requirements.txt yet. Both engines are
    tried, and the reason is handed back rather than swallowed: a download
    button that quietly changes format is a bug report that takes an hour,
    and the reason turns it into one line on screen.

    The header row is frozen and filterable and the columns are widened to
    their contents, because this file is opened to be read and sorted.
    """
    reasons = []
    for engine in ("openpyxl", "xlsxwriter"):
        buffer = io.BytesIO()
        try:
            with pd.ExcelWriter(buffer, engine=engine) as writer:
                frame.to_excel(writer, index=False, sheet_name=sheet[:31])
                worksheet = writer.sheets[sheet[:31]]
                widths = [
                    min(max([len(str(column))]
                            + [len(str(value)) for value in frame[column].head(500)]) + 2,
                        60)
                    for column in frame.columns
                ]
                if engine == "openpyxl":
                    worksheet.freeze_panes = "A2"
                    worksheet.auto_filter.ref = worksheet.dimensions
                    for position, width in enumerate(widths, start=1):
                        worksheet.column_dimensions[
                            worksheet.cell(row=1, column=position).column_letter
                        ].width = width
                else:
                    worksheet.freeze_panes(1, 0)
                    if len(frame):
                        worksheet.autofilter(0, 0, len(frame), len(frame.columns) - 1)
                    for position, width in enumerate(widths):
                        worksheet.set_column(position, position, width)
            return buffer.getvalue(), ""
        except Exception as exc:  # noqa: BLE001 - try the next engine, then report
            reasons.append(f"{engine}: {type(exc).__name__}: {exc}")
    return b"", "; ".join(reasons)


def truthy(frame: pd.DataFrame, column: str) -> pd.Series:
    """A yes/true/1 column as a boolean, False where the column is absent."""
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].astype(str).str.strip().str.lower().isin(
        ("yes", "true", "1"))


def populated(frame: pd.DataFrame, column: str) -> pd.Series:
    """Is this column filled in? False where the column is absent."""
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].astype(str).str.strip().ne("")


def full_view_users() -> set:
    """
    Who may see the whole working list.

    `[ko_monitoring] full_view_users` in the secrets, as a list or a
    comma-separated string, on top of the default. Administrators always may,
    so a new administrator needs no secrets change.
    """
    configured = st.secrets.get(APP_KEY, {}).get("full_view_users", [])
    if isinstance(configured, str):
        configured = [part.strip() for part in configured.split(",")]
    return {str(name).strip().lower() for name in
            list(DEFAULT_FULL_VIEW_USERS) + list(configured) if str(name).strip()}


def choose_view(username: str) -> str:
    """
    `tlg` or `debug`, and the picker when the user is allowed both.

    A user who may only see one is not shown a choice — the picker would only
    be a locked door with a label on it. The default is GXO's list: it is the
    list the work is done from, and troubleshooting is for when a row on it
    looks wrong.
    """
    may_see_all = str(username).strip().lower() in full_view_users() or is_admin()
    if not may_see_all:
        return "tlg"
    labels = {
        "tlg": "GXO's list — every shipment still open at GXO",
        "debug": "Troubleshooting — every row in the table, nothing hidden",
    }
    picked = st.radio(
        "View", list(labels), format_func=labels.get, horizontal=True,
        key="ko_view", index=0,
        help="The GXO view is what a TLG login sees.",
    )
    return picked


def column_filters(frame: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
    """
    A filter per column, laid out three to a row.

    A column with few enough distinct values gets a multiselect, because
    picking from a list beats typing; anything else gets a "contains" box.
    Dates and timestamps get a box too — `2026-11` is a readable way to ask
    for a month, and a date picker on a column that is often empty is worse
    than useless.
    """
    shown = frame
    present = [column for column in columns if column in frame.columns]
    for index in range(0, len(present), 3):
        cells = st.columns(3)
        for cell, column in zip(cells, present[index:index + 3]):
            values = options(frame, column)
            with cell:
                if 0 < len(values) <= 40:
                    picked = st.multiselect(column, values, key=f"f_{column}")
                    if picked:
                        shown = shown[shown[column].isin(picked)]
                else:
                    needle = st.text_input(
                        column, key=f"f_{column}",
                        placeholder="contains…" if values else "nothing to filter",
                        disabled=not values,
                    )
                    if needle.strip():
                        shown = shown[shown[column].astype(str).str.contains(
                            needle.strip(), case=False, na=False, regex=False)]
    return shown


def options(frame: pd.DataFrame, column: str) -> List[str]:
    if column not in frame.columns:
        return []
    return sorted({str(value).strip() for value in frame[column] if str(value).strip()})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def tab_dashboard(config, username: str) -> None:
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

    everything = frame
    view = choose_view(username)

    # Troubleshooting shows the table as it is. Every other view is a working
    # list, and a working list drops the rows nobody can act on: the resolved
    # ones, which the workflow archives out of the table anyway and which are
    # only still here after a run that could not finish clearing them, and the
    # ones blocked behind another shipment's stock. Those two cuts are exactly
    # what makes a row "missing" from a dashboard, so the view that exists to
    # explain a row must not make them.
    if view != "debug":
        if "ko_resolved" in frame.columns:
            frame = frame[~truthy(frame, "ko_resolved")]
        if "blocking_reason" in frame.columns:
            frame = frame[~frame["blocking_reason"].astype(str).str.strip()
                          .str.startswith(STOCK_RESERVED)]

    if view == "tlg":
        # GXO's list, and only GXO's: every shipment at that warehouse, tried
        # or not. A shipment nobody has asked GXO about yet is still GXO's to
        # handle, so the only thing this view takes out is another warehouse.
        gxo = pd.Series(False, index=frame.index)
        if "location_provider" in frame.columns:
            gxo |= frame["location_provider"].str.strip().str.upper().eq("GXO")
        if "source" in frame.columns:
            gxo |= frame["source"].str.upper().str.contains("GXO", na=False)
        frame = frame[gxo]
    # "debug" filters nothing: it is the table, and the filter grid below is
    # how you narrow it.
    frame = frame.reset_index(drop=True)

    if loaded.get("origin") == "table":
        # The table's own size, not the filtered count: "ever seen" meant the
        # number left after the view had already taken its cut, which made a
        # view that hides rows look like a table that has not got them.
        st.caption(
            f"{ko_table.FQN} · {len(everything)} row(s) in the table · "
            + {"tlg": "GXO's list",
               "debug": "troubleshooting, nothing hidden"}.get(view, view)
            + f" · read {ko.show_time(loaded['read_at'].isoformat())} UTC"
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

    fixed = populated(frame, "ko_fix_date") | truthy(frame, "ko_fixed")

    # The three groups the work actually divides into, read off the action
    # column. Everything with no action is still ours to look at: the workflow
    # has not found anything for GXO to do, so it is sitting with TLG.
    action = (frame["action_needed_from_gxo"].astype(str).str.strip()
              if "action_needed_from_gxo" in frame.columns
              else pd.Series("", index=frame.index))
    to_print = action.eq(ACTION_PRINT)
    to_cancel = action.eq(ACTION_CANCEL)
    with_tlg = ~(to_print | to_cancel)

    # Four numbers, and the last three add up to the first. ko_fixed had a
    # metric of its own here and lost it: it is this workflow's verdict on the
    # documents call, not a pile of work, and next to three counts of things
    # somebody has to go and do it only invited the question of why the four
    # did not reconcile.
    cells = st.columns(4)
    cells[0].metric("Shipments", len(frame))
    cells[1].metric(ACTION_PRINT, int(to_print.sum()))
    cells[2].metric("Cancellation requested", int(to_cancel.sum()))
    cells[3].metric("Still checking on TLG side", int(with_tlg.sum()))

    with st.expander("Filters", expanded=False):
        shown = column_filters(frame, wanted_columns(frame, view))
        row = st.columns([3, 1])
        needle = row[0].text_input(
            "Search every column", key="f_search",
            placeholder="shipment, OERODP, logistic order, tracking number…",
        )
        show_evidence = row[1].checkbox("Show the evidence columns", value=False)
        if needle.strip():
            text = needle.strip().upper()
            shown = shown[shown.apply(
                lambda record: any(text in str(value).upper()
                                   for value in record.values), axis=1)]

    wanted = wanted_columns(shown, view)
    if show_evidence:
        wanted += [column for column in EVIDENCE_COLUMNS
                   if column in shown.columns and column not in wanted]

    st.caption(f"{len(shown)} of {len(frame)} shipment(s)"
               + (f" · {len(everything) - len(frame)} row(s) outside this view"
                  if len(everything) > len(frame) else ""))
    st.dataframe(shown[wanted], use_container_width=True, hide_index=True, height=520)
    workbook, why_not_excel = excel_bytes(shown[wanted])
    if workbook:
        st.download_button(
            "Download what is on screen",
            workbook,
            file_name=f"ko_monitoring-{view}-{ko.stamp()}.xlsx",
            mime=XLSX_MIME,
        )
    else:
        st.download_button(
            "Download what is on screen",
            shown[wanted].to_csv(index=False).encode("utf-8"),
            file_name=f"ko_monitoring-{view}-{ko.stamp()}.csv",
            mime="text/csv",
        )
        st.caption("This is a CSV because the workbook could not be built: "
                   + why_not_excel
                   + " — `openpyxl` is in requirements.txt; if it is named "
                     "there, the deployment has not reinstalled yet.")

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
        "Upload either file, or both. Whatever you upload goes into one commit, so "
        "the workflow starts once with the batch already complete. It then checks "
        "every shipment against the documents API, which takes a few minutes for a "
        "few hundred rows."
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

    ready = csv_file is not None or xlsx_file is not None
    if not ready:
        st.info("Upload the CSV, the workbook, or both.")
    elif csv_file is None:
        # Worth saying plainly: the archiving rule turns on this, and a run
        # that quietly archived nothing would look like a run that did nothing.
        st.caption("Workbook only. GXO's shipments will be re-checked and any new "
                   "ones added, but none will be archived for being absent — a "
                   "missing CSV is not evidence that a shipment has gone.")
    elif xlsx_file is None:
        st.caption("CSV only. No GXO shipment will be archived for being absent, "
                   "because that is only ever decided against a workbook.")

    if st.button("Queue the run", type="primary", disabled=not ready, use_container_width=True):
        batch = ko.stamp()
        user = "".join(character if character.isalnum() else "-" for character in username)[:40] or "user"
        files = []
        if csv_file is not None:
            files.append((f"{config.input_dir}/{batch}__{user}__pending.csv",
                          csv_file.getvalue()))
        if xlsx_file is not None:
            suffix = ".xlsm" if xlsx_file.name.lower().endswith(".xlsm") else ".xlsx"
            files.append((f"{config.input_dir}/{batch}__{user}__check{suffix}",
                          xlsx_file.getvalue()))
        with st.spinner(f"Committing {'both files' if len(files) == 2 else 'the file'}…"):
            sha, error = ko.commit_files(
                github, files, f"KO monitoring batch {batch} from {username}"
            )
        if error:
            st.error(f"The upload could not be committed: {error}")
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
        tab_dashboard(config, username)
    with new_run:
        tab_new_run(config, github, username)
    with runs_tab:
        tab_runs(config, github)
    with log:
        from shared.console import tab_logs
        tab_logs(github, config)


# This file is a page of streamlit_app.py, so it runs on import by st.navigation.
main()
