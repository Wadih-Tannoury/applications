#!/usr/bin/env python3
"""
Warehouse cancellation console.

Drops cancellation CSV files into the warehouse queue of the operational_tools
repository (warehouse/input/), where the Warehouse cancellations workflow picks
them up and sends a CANCEL event to the GXO endpoint, and reads the run logs
back out.

Each record cancels at the warehouse, and says whether Kibo is to follow:

    Warehouse only      the GXO event, and nothing in Kibo.
    Warehouse, then Kibo    the GXO event first; the shipment is cancelled in
                        Kibo only once the warehouse has accepted it.

The warehouse event stops a whole shipment and carries an empty products array:
it has no item granularity, so this console deals in shipments only — and so
does the Kibo cancellation that follows it. Cancelling a single item is the
Kibo cancellations page's job.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

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

CSV_COLUMNS = ["shipmentOrderNumber", "reasonMessage", "cancelInKibo"]

# Columns every row has to carry, with the label shown when one is missing.
REQUIRED = {"shipmentOrderNumber": "shipmentOrderNumber", "reasonMessage": "reasonMessage"}

ALIASES = {
    "shipmentordernumber": "shipmentOrderNumber",
    "shipmentorder": "shipmentOrderNumber",
    "son": "shipmentOrderNumber",
    "reasonmessage": "reasonMessage",
    "moreinfo": "reasonMessage",
    "reason": "reasonMessage",
    "cancelinkibo": "cancelInKibo",
    "cancelkibo": "cancelInKibo",
    "alsokibo": "cancelInKibo",
    "kibo": "cancelInKibo",
}

DEFAULT_REASON_MESSAGE = "Kibo notification cancellation"

# What the two options mean, in one place: the radio, the preview and the
# button all read from here, so they cannot drift apart.
WAREHOUSE_ONLY = "Warehouse only"
WAREHOUSE_THEN_KIBO = "Warehouse, then Kibo"

SCOPE_CAPTIONS = {
    WAREHOUSE_ONLY: (
        "The CANCEL event goes to the warehouse and stops the shipment there. "
        "Kibo is left exactly as it is."
    ),
    WAREHOUSE_THEN_KIBO: (
        "The CANCEL event goes to the warehouse **first**. Only once the warehouse has "
        "accepted it is the whole shipment cancelled in Kibo. If the warehouse event "
        "fails, nothing is cancelled in Kibo."
    ),
}

SCOPE_BUTTONS = {
    WAREHOUSE_ONLY: "Cancel at the warehouse",
    WAREHOUSE_THEN_KIBO: "Cancel at the warehouse, then in Kibo",
}

# What goes in the cancelInKibo column of the queued CSV. The worker reads
# yes / no, and a blank cell means no.
SCOPE_FLAGS = {WAREHOUSE_ONLY: "no", WAREHOUSE_THEN_KIBO: "yes"}

TRUE_WORDS = {"y", "yes", "true", "t", "1", "x", "si", "kibo", "both"}


def scope_picker(key: str) -> str:
    """The two options, as a radio. Returns the chosen label."""
    choice = st.radio(
        "What should be cancelled",
        [WAREHOUSE_ONLY, WAREHOUSE_THEN_KIBO],
        key=key,
        horizontal=True,
        captions=[SCOPE_CAPTIONS[WAREHOUSE_ONLY], SCOPE_CAPTIONS[WAREHOUSE_THEN_KIBO]],
    )
    return choice or WAREHOUSE_ONLY


def scope_note(choice: str) -> None:
    """The one thing neither option can do: stop part of a shipment."""
    tail = (
        " The Kibo cancellation that follows is the whole shipment too."
        if choice == WAREHOUSE_THEN_KIBO
        else ""
    )
    st.info(
        "The warehouse event stops a **whole shipment**: it carries no products, so there "
        "is no way to stop part of one." + tail + " To cancel a single item, use the "
        "**Kibo cancellations** page instead — the warehouse keeps packing whatever is left.",
        icon="📦",
    )


def apply_scope(rows: List[Dict[str, str]], choice: str) -> Tuple[List[Dict[str, str]], int]:
    """
    Write the chosen scope into every row that does not carry one of its own.

    A cancelInKibo column in the uploaded file wins: someone who took the
    trouble to set it per row means it. Returns (rows, how many go to Kibo).
    """
    flag = SCOPE_FLAGS[choice]
    for row in rows:
        own = str(row.get("cancelInKibo", "")).strip()
        row["cancelInKibo"] = own or flag
    going_to_kibo = sum(
        1 for row in rows
        if str(row.get("cancelInKibo", "")).strip().lower().replace(" ", "") in TRUE_WORDS
    )
    return rows, going_to_kibo


def missing_required(rows: List[Dict[str, str]]) -> List[str]:
    """
    Complaints about rows that cannot be sent as they stand.

    The warehouse event carries data.reasonMessage, and what it says is the only
    explanation anyone downstream gets — it is also what the Kibo cancellation
    records as moreInfo — so a row without one is refused here rather than sent
    with a filler.
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


def confirm_kibo(key: str) -> bool:
    """
    The Kibo leg is the irreversible half, so it is ticked, not assumed.
    """
    return st.checkbox(
        "I understand the whole shipment will also be cancelled in Kibo.",
        key=key,
    )


def summary(count: int, going_to_kibo: int, choice: str) -> None:
    if choice == WAREHOUSE_THEN_KIBO:
        st.caption(
            f"{count} shipment(s) ready: all {count} cancelled at the warehouse, "
            f"{going_to_kibo} then cancelled in Kibo."
        )
    else:
        st.caption(f"{count} shipment(s) ready to cancel in full at the warehouse"
                   + (f"; {going_to_kibo} row(s) ask for Kibo too, from the file's own "
                      "cancelInKibo column." if going_to_kibo else "."))


def tab_upload(github: GitHub, config, username: str) -> None:
    st.markdown("#### Upload a CSV")
    st.caption(
        "Columns: shipmentOrderNumber and reasonMessage, both required. "
        "An optional cancelInKibo column (yes / no) overrides the choice below, row by row. "
        "Every row cancels the whole shipment."
    )

    choice = scope_picker("scope_upload")
    scope_note(choice)

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], label_visibility="collapsed")
    if uploaded is None:
        with st.expander("Example file"):
            st.code(
                "shipmentOrderNumber,reasonMessage,cancelInKibo\n"
                "OW-USA00037769,Kibo notification cancellation,no\n"
                "OW-USA00037770,Cancelled at source,yes\n"
                "DJ-ITA00012345,Customer changed their mind,\n",
                language="text",
            )
            st.caption(
                "The last row leaves cancelInKibo empty, so it follows whichever option "
                "is chosen above."
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

    rows, going_to_kibo = apply_scope(rows, choice)

    st.dataframe(rows, use_container_width=True, hide_index=True)
    summary(len(rows), going_to_kibo, choice)

    blocking = missing_required(rows)
    if blocking:
        for problem in blocking:
            st.error(problem)
        st.caption(
            "Fix the file and upload it again. Nothing is queued while a row is incomplete."
        )
        return

    acknowledged = True
    if going_to_kibo:
        st.warning(
            f"{going_to_kibo} of {len(rows)} shipment(s) will be cancelled in Kibo as well, "
            "once the warehouse has taken them.",
            icon="⚠️",
        )
        acknowledged = confirm_kibo("confirm_upload")

    if st.button(SCOPE_BUTTONS[choice], type="primary", disabled=not acknowledged):
        submit_rows(github, config, rows, CSV_COLUMNS, username,
                    uploaded.name.rsplit(".", 1)[0], "Warehouse cancellation request")
        if going_to_kibo:
            st.caption(
                "The worker sends the warehouse event first and only cancels in Kibo "
                "after it has been accepted. Both steps are in the run log."
            )


def tab_single(github: GitHub, config, username: str) -> None:
    st.markdown("#### Cancel one shipment")
    st.caption("The entry is turned into a one-row CSV and dropped in the input folder.")

    choice = scope_picker("scope_single")
    scope_note(choice)

    if choice == WAREHOUSE_THEN_KIBO:
        acknowledged = confirm_kibo("confirm_single")
    else:
        acknowledged = True

    with st.form("single"):
        shipment_order_number = st.text_input(
            "shipmentOrderNumber", placeholder="OW-USA00037769"
        )
        reason_message = st.text_input(
            "Reason message (required)",
            placeholder=DEFAULT_REASON_MESSAGE,
            help="Goes out as data.reasonMessage on the warehouse event, and as moreInfo "
                 "on the Kibo cancellation when one follows.",
        )
        submitted = st.form_submit_button(
            SCOPE_BUTTONS[choice], type="primary", disabled=not acknowledged
        )

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
                "cancelInKibo": SCOPE_FLAGS[choice],
            }
        ],
        CSV_COLUMNS,
        username,
        "shipment",
        "Warehouse cancellation request",
    )
    if choice == WAREHOUSE_THEN_KIBO:
        st.caption(
            "The worker sends the warehouse event first and only cancels in Kibo after it "
            "has been accepted. Both steps are in the run log."
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
