#!/usr/bin/env python3
"""
Warehouse cancellation console.

Drops cancellation CSV files into the warehouse queue of the operational_tools
repository (warehouse/input/), where the Warehouse cancellations workflow picks
them up and sends a CANCEL event to the GXO endpoint, and reads the run logs
back out.

The warehouse event stops a whole shipment and carries an empty products array:
it has no item granularity, so this console deals in shipments only. Cancelling
a single item is the Kibo cancellations page's job.
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

CSV_COLUMNS = ["shipmentOrderNumber", "reasonMessage"]

# Columns every row has to carry, with the label shown when one is missing.
REQUIRED = {"shipmentOrderNumber": "shipmentOrderNumber", "reasonMessage": "reasonMessage"}

ALIASES = {
    "shipmentordernumber": "shipmentOrderNumber",
    "shipmentorder": "shipmentOrderNumber",
    "son": "shipmentOrderNumber",
    "reasonmessage": "reasonMessage",
    "moreinfo": "reasonMessage",
    "reason": "reasonMessage",
}

DEFAULT_REASON_MESSAGE = "Kibo notification cancellation"


def missing_required(rows: List[Dict[str, str]]) -> List[str]:
    """
    Complaints about rows that cannot be sent as they stand.

    The warehouse event carries data.reasonMessage, and what it says is the only
    explanation anyone downstream gets, so a row without one is refused here
    rather than sent with a filler.
    """
    problems = []
    for position, row in enumerate(rows, start=1):
        absent = [label for column, label in REQUIRED.items() if not row.get(column)]
        if absent:
            problems.append(
                f"Record {position} "
                f"({row.get('shipmentOrderNumber') or 'no shipmentOrderNumber'}) "
                "is missing " + " and ".join(absent) + "."
            )
    return problems


def scope_note() -> None:
    st.info(
        "The warehouse event stops a **whole shipment**: it carries no products, so there "
        "is no way to stop part of one. To cancel a single item, use the **Kibo "
        "cancellations** page instead — the warehouse keeps packing whatever is left.",
        icon="📦",
    )


def tab_upload(github: GitHub, config, username: str) -> None:
    st.markdown("#### Upload a CSV")
    st.caption(
        "Columns: shipmentOrderNumber and reasonMessage, both required. "
        "Every row cancels the whole shipment."
    )
    scope_note()

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], label_visibility="collapsed")
    if uploaded is None:
        with st.expander("Example file"):
            st.code(
                "shipmentOrderNumber,reasonMessage\n"
                "OW-USA00037769,Kibo notification cancellation\n"
                "OW-USA00037770,Cancelled at source\n"
                "DJ-ITA00012345,Customer changed their mind\n",
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

    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} shipment(s) ready to cancel in full.")

    blocking = missing_required(rows)
    if blocking:
        for problem in blocking:
            st.error(problem)
        st.caption(
            "Fix the file and upload it again. Nothing is queued while a row is incomplete."
        )
        return

    if st.button("Submit Cancellations", type="primary"):
        submit_rows(github, config, rows, CSV_COLUMNS, username,
                    uploaded.name.rsplit(".", 1)[0], "Warehouse cancellation request")


def tab_single(github: GitHub, config, username: str) -> None:
    st.markdown("#### Cancel one shipment at the warehouse")
    st.caption("The entry is turned into a one-row CSV and dropped in the input folder.")
    scope_note()

    with st.form("single"):
        shipment_order_number = st.text_input(
            "shipmentOrderNumber", placeholder="OW-USA00037769"
        )
        reason_message = st.text_input(
            "Reason message (required)",
            placeholder=DEFAULT_REASON_MESSAGE,
            help="Goes out as data.reasonMessage on the warehouse event.",
        )
        submitted = st.form_submit_button("Submit Cancellation", type="primary")

    if not submitted:
        return

    shipment_order_number = shipment_order_number.strip()
    if not shipment_order_number:
        st.error("Enter a shipmentOrderNumber.")
        return
    if not reason_message.strip():
        st.error("Enter a reason message: it is what the warehouse event carries.")
        return

    submit_rows(
        github,
        config,
        [
            {
                "shipmentOrderNumber": shipment_order_number,
                "reasonMessage": reason_message.strip(),
            }
        ],
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
