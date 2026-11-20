#!/usr/bin/env python3
"""
Warehouse cancellation console.

Drops cancellation CSV files into the warehouse queue of the operational_tools
repository (warehouse/input/), where the Warehouse cancellations workflow picks
them up and sends a CANCEL event to the GXO endpoint, and reads the run logs
back out.

This console cancels at the warehouse and nowhere else. Every row is queued
with cancelInKibo=no, so the shipment stays exactly as it is in Kibo whatever
the workflow's own Kibo switch is set to; cancelling in Kibo is the Kibo
cancellations page's job.

Two things are asked for: the shipment number, and why it is being cancelled.
The warehouse event itself carries a fixed reasonMessage — the warehouse has no
use for a per-shipment explanation — but reasonCode and moreInfo are written to
the cancellation ledger, and the reconciler reads them back hours later when it
cancels the shipment in Kibo. So the reason chosen here is the reason Kibo
ends up recording.

moreInfo is optional and goes to Kibo empty when nobody fills it in, except on
"Other", where an empty moreInfo would tell Kibo nothing: there it is sent as
"Other" too.

The warehouse event stops a whole shipment and carries an empty products array:
it has no item granularity, so this console deals in shipments only. Cancelling
a single item is the Kibo cancellations page's job too.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.console import (  # noqa: E402
    GitHub,
    parse_uploaded_csv,
    settings,
    start_page,
    submit_rows,
    tab_logs,
    tab_queue,
)

APP_KEY = "warehouse"
DEFAULT_FOLDERS = {
    "input_dir": "warehouse/input",
    "logs_dir": "warehouse/logs",
    "archive_dir": "warehouse/archive",
    "error_dir": "warehouse/error",
}

CSV_COLUMNS = ["shipmentOrderNumber", "cancelInKibo", "reasonCode", "moreInfo"]

ALIASES = {
    "shipmentordernumber": "shipmentOrderNumber",
    "shipmentorder": "shipmentOrderNumber",
    "son": "shipmentOrderNumber",
    "reasoncode": "reasonCode",
    "reason": "reasonCode",
    "cancellationreason": "reasonCode",
    "moreinfo": "moreInfo",
    "info": "moreInfo",
    "notes": "moreInfo",
}

# The reasons Kibo accepts. The same list the workers hold, and the only values
# this console will queue: a CSV naming anything else is refused here rather
# than rejected by Kibo hours later.
REASON_CODES = [
    "Arrived Too Late",
    "Can Not Find Item",
    "Customer Changed Their Mind",
    "Damaged Or Defective Item",
    "Do Not Have Enough Quantity",
    "Found Better Price",
    "Item Did Not Match Website",
    "Item Is On Reserve For Other Customer",
    "Item Is Shop Worn Not Web Sellable",
    "Item Not Available",
    "Item Price Too High",
    "Need To Change Payment Method",
    "No Inventory",
    "Other",
]

OTHER_REASON = "Other"

# Written on every row. The worker reads yes / no, and this console only ever
# says no: what it queues is a warehouse cancellation, full stop.
CANCEL_IN_KIBO = "no"

# The warehouse event's data.reasonMessage, the same on every record. The
# worker sends this when a row carries no reasonMessage of its own, and this
# console never sends one.
FIXED_REASON_MESSAGE = "Cancelled"

BUTTON = "Cancel at the warehouse"


def scope_note() -> None:
    """What this page does and does not touch."""
    st.info(
        "The warehouse event stops a **whole shipment**: it carries no products, so there "
        "is no way to stop part of one, and nothing is cancelled in Kibo. To cancel a "
        "single item, or to cancel in Kibo, use the **Kibo cancellations** page — the "
        "warehouse keeps packing whatever is left.",
        icon="📦",
    )
    st.caption(
        f"The warehouse event always goes out with reasonMessage “{FIXED_REASON_MESSAGE}”; "
        "it is not asked for here. The reason code below is what Kibo records when the "
        "shipment is cancelled there."
    )


def apply_scope(rows: List[Dict[str, str]], reason_code: str = "",
                more_info: str = "") -> List[Dict[str, str]]:
    """
    Queue every row as a warehouse-only cancellation, with a reason on it.

    A row that names its own reasonCode keeps it; the rest take the one chosen
    on the page, so a plain one-column CSV still works.
    """
    for row in rows:
        row["cancelInKibo"] = CANCEL_IN_KIBO
        row["reasonCode"] = (row.get("reasonCode") or reason_code).strip()
        row["moreInfo"] = (row.get("moreInfo") or more_info).strip()
        # An "Other" with nothing beside it tells Kibo nothing, so it carries
        # the word itself.
        if row["reasonCode"] == OTHER_REASON and not row["moreInfo"]:
            row["moreInfo"] = OTHER_REASON
    return rows


def missing_required(rows: List[Dict[str, str]]) -> List[str]:
    """
    Complaints about rows that cannot be sent as they stand.

    shipmentOrderNumber is what the warehouse cancellation is keyed on, so a
    row without one is refused here rather than queued to fail in the workflow.
    """
    problems = [
        f"Record {position} is missing shipmentOrderNumber."
        for position, row in enumerate(rows, start=1)
        if not row.get("shipmentOrderNumber")
    ]
    problems += [
        f"Record {position} has no reasonCode."
        for position, row in enumerate(rows, start=1)
        if not row.get("reasonCode")
    ]
    # A reason Kibo does not know would be refused hours later, by the
    # reconciler, where nobody is watching. Better to refuse it here.
    problems += [
        f"Record {position} has reasonCode “{row['reasonCode']}”, which is not one "
        "of the accepted reasons."
        for position, row in enumerate(rows, start=1)
        if row.get("reasonCode") and row["reasonCode"] not in REASON_CODES
    ]
    return problems


def tab_upload(github: GitHub, config, username: str) -> None:
    st.markdown("#### Upload a CSV")
    st.caption(
        "One column is enough: shipmentOrderNumber. Every row cancels the whole shipment "
        "at the warehouse, with the reason chosen below."
    )
    scope_note()

    left, right = st.columns([1, 1])
    reason_code = left.selectbox(
        "Reason code", REASON_CODES, index=None, placeholder="Choose a reason…",
        help="Applies to every row that does not carry its own reasonCode column.",
    )
    more_info = right.text_input(
        "More info (optional)", placeholder="Anything Kibo should record beside the reason",
    )
    if reason_code == OTHER_REASON and not more_info.strip():
        st.caption("“Other” with nothing beside it is sent to Kibo as moreInfo “Other”.")

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], label_visibility="collapsed")
    if uploaded is None:
        with st.expander("Example file"):
            st.code(
                "shipmentOrderNumber\n"
                "OW-USA00037769\n"
                "OW-USA00037770\n"
                "DJ-ITA00012345\n",
                language="text",
            )
            st.caption(
                "A file can also carry its own reasonCode and moreInfo columns, which "
                "take priority over the choice above — useful when one upload covers "
                "several reasons."
            )
            st.code(
                "shipmentOrderNumber,reasonCode,moreInfo\n"
                "OW-USA00037769,No Inventory,\n"
                "DJ-ITA00012345,Other,Chargeback opened\n",
                language="text",
            )
        return

    rows, problems = parse_uploaded_csv(
        uploaded.getvalue(),
        ALIASES,
        CSV_COLUMNS,
        ["shipmentOrderNumber"],
        "The header needs a shipmentOrderNumber column: the warehouse cancellation is "
        "keyed on it.",
    )
    for problem in problems:
        st.warning(problem)
    if not rows:
        return

    rows = apply_scope(rows, reason_code or "", more_info)

    if not reason_code and any(not row.get("reasonCode") for row in rows):
        st.warning(
            "Choose a reason code above, or give the file its own reasonCode column. "
            "Nothing is queued without one."
        )

    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} shipment(s) ready to cancel in full at the warehouse.")

    blocking = missing_required(rows)
    if blocking:
        for problem in blocking:
            st.error(problem)
        st.caption(
            "Fix the file and upload it again. Nothing is queued while a row is incomplete."
        )
        return

    if st.button(BUTTON, type="primary"):
        submit_rows(github, config, rows, CSV_COLUMNS, username,
                    uploaded.name.rsplit(".", 1)[0], "Warehouse cancellation request")


def tab_single(github: GitHub, config, username: str) -> None:
    st.markdown("#### Cancel one shipment")
    st.caption("The entry is turned into a one-row CSV and dropped in the input folder.")
    scope_note()

    with st.form("single"):
        shipment_order_number = st.text_input(
            "shipmentOrderNumber", placeholder="OW-USA00037769"
        )
        reason_code = st.selectbox(
            "Reason code", REASON_CODES, index=None, placeholder="Choose a reason…",
            help="What Kibo records when the shipment is cancelled there.",
        )
        more_info = st.text_input(
            "More info (optional)",
            placeholder="Anything Kibo should record beside the reason",
        )
        submitted = st.form_submit_button(BUTTON, type="primary")

    if not submitted:
        return

    shipment_order_number = shipment_order_number.strip()
    if not shipment_order_number:
        st.error("Enter a shipmentOrderNumber.")
        return
    if not reason_code:
        st.error("Choose a reason code.")
        return

    rows = apply_scope(
        [{"shipmentOrderNumber": shipment_order_number}], reason_code, more_info
    )
    if rows[0]["moreInfo"] == OTHER_REASON and not more_info.strip():
        st.caption("No more info given, so Kibo receives moreInfo “Other”.")

    submit_rows(
        github,
        config,
        rows,
        CSV_COLUMNS,
        username,
        "shipment",
        "Warehouse cancellation request",
    )


def main() -> None:
    # set_page_config lives in streamlit_app.py: it runs once for the session,
    # and this file is one page of that app.
    config = settings(APP_KEY, DEFAULT_FOLDERS)
    username = start_page("Warehouse cancellation console", config,
                          app_key=APP_KEY, label="Warehouse cancellations")
    github = GitHub(config)

    upload, single, logs, queue = st.tabs(
        ["Upload CSV", "Single cancellation", "Logs", "Folders"]
    )
    with upload:
        tab_upload(github, config, username)
    with single:
        tab_single(github, config, username)
    with logs:
        tab_logs(github, config)
    with queue:
        tab_queue(github, config)


# This file is a page of streamlit_app.py, so it runs on import by st.navigation.
main()
