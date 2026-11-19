#!/usr/bin/env python3
"""
Cancellation ledger console.

Shows `tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table —
every shipment the operational_tools repository has cancelled at the GXO
warehouse, and what became of it since.

This page is read-only. It queues nothing, sends nothing and writes nothing; it
is the window onto what the two cancellation workflows have done. The table is
read afresh on every run of this script, which Streamlit does on sign-in, on
refresh and whenever a filter here changes, so what is on screen is what
BigQuery holds at that moment rather than something cached from earlier.

The ledger fills in three stages, and the Stage column says which one a shipment
has reached:

    Not sent               the cancellation never left for the warehouse
    Awaiting the warehouse sent, no CANCELLED event back yet
    To cancel in Kibo      the warehouse confirmed it; Kibo is still open
    Done                   cancelled at the warehouse and in Kibo

A row still awaiting the warehouse after 24 hours is marked overdue: that is the
same window the Reconcile cancellations workflow uses, so an overdue row here is
one that workflow will resend on its next run.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.console import rows_to_csv, start_page  # noqa: E402
from shared import ledger as ledger_module  # noqa: E402
from shared.ledger import (  # noqa: E402
    COLUMNS, FQN, TIMESTAMP_COLUMNS, fetch_rows, overdue, stage_of, summarise,
)

APP_KEY = "ledger"
LABEL = "Cancellation ledger"

STAGES = ["Not sent", "Awaiting the warehouse", "To cancel in Kibo", "Done"]

WINDOWS = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "Everything": 0,
}


def show_timestamp(value: Any) -> str:
    """A timestamp as the page shows it: minutes, UTC, no microseconds."""
    if not isinstance(value, datetime):
        return "" if value is None else str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def show_bool(value: Any) -> str:
    return "yes" if value else "no"


def display_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The ledger as the table renders it: one dict per row, headings as keys.

    Booleans become yes/no and timestamps become minutes so the columns stay
    narrow enough to read across, and Stage is put first because it is the
    column anyone opening this page is actually looking for.
    """
    shown: List[Dict[str, Any]] = []
    for row in rows:
        entry: Dict[str, Any] = {"Stage": stage_of(row)}
        if overdue(row):
            entry["Stage"] = "Awaiting the warehouse ⚠"
        for name, heading in COLUMNS:
            value = row.get(name)
            if name in TIMESTAMP_COLUMNS:
                entry[heading] = show_timestamp(value)
            elif isinstance(value, bool):
                entry[heading] = show_bool(value)
            else:
                entry[heading] = "" if value is None else value
        shown.append(entry)
    return shown


def main() -> None:
    # set_page_config lives in streamlit_app.py: it runs once for the session,
    # and this file is one page of that app.
    start_page(
        "Cancellation ledger",
        caption=f"{FQN} · read-only · re-read on every refresh",
        app_key=APP_KEY,
        label=LABEL,
    )

    controls_left, controls_middle, controls_right = st.columns([2, 3, 1])
    window = controls_left.selectbox("Period", list(WINDOWS), index=1,
                                     help="Filtered on the row's last update.")
    search = controls_middle.text_input(
        "Search", placeholder="Shipment number or item code",
        help="Matches part of a shipmentOrderNumber or of the items column.",
    )
    controls_right.write("")
    # Streamlit re-runs the script on every refresh, so the table is already
    # current when the page loads. The button is here for someone watching a
    # cancellation move through the stages without touching anything else.
    controls_right.button("Refresh", use_container_width=True)

    result = fetch_rows(days=WINDOWS[window], search=search)

    if not result.ok:
        st.error(result.error)
        st.caption(
            "The ledger is written by the operational_tools repository. If it has "
            "never run since the table was created, there is nothing to show yet."
        )
        return

    if result.missing_columns:
        st.warning(
            "The table does not have "
            + ", ".join(f"`{name}`" for name in result.missing_columns)
            + ". Those columns show as empty. Run "
            "`sql/warehouse_orders_cancellation_table.sql` in operational_tools."
        )

    rows = result.rows
    stage_filter = st.multiselect(
        "Stage", STAGES, default=[],
        help="Leave empty to show every stage.",
    )
    if stage_filter:
        rows = [row for row in rows if stage_of(row) in stage_filter]

    counts = summarise(rows)
    first, second, third, fourth = st.columns(4)
    first.metric("Shipments", counts["rows"])
    second.metric("Awaiting the warehouse", counts["awaiting"])
    third.metric("To cancel in Kibo", counts["to_cancel_in_kibo"])
    fourth.metric("Done", counts["cancelled_in_kibo"])

    late = sum(1 for row in rows if overdue(row))
    if late:
        st.info(
            f"{late} cancellation(s) have gone more than 24 hours without a CANCELLED "
            "event from the warehouse, marked ⚠ below. The Reconcile cancellations "
            "workflow sends those again on its next run."
        )

    if not rows:
        st.caption("No shipment matches these filters.")
    else:
        st.dataframe(display_rows(rows), use_container_width=True, hide_index=True)

    footer_left, footer_right = st.columns([3, 1])
    footer_left.caption(
        f"Read at {result.read_at.strftime('%Y-%m-%d %H:%M:%S')} UTC · "
        f"{len(result.rows)} row(s) from the ledger"
        + (f", {len(rows)} shown" if len(rows) != len(result.rows) else "")
        + ". Times are UTC."
    )
    if rows:
        shown = display_rows(rows)
        footer_right.download_button(
            "Download CSV",
            data=rows_to_csv(shown, list(shown[0].keys())),
            file_name=f"cancellation-ledger-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with st.expander("What the stages mean"):
        st.markdown(
            """
**Not sent** — the shipment is on the ledger but the CANCEL event never reached
the warehouse. The run that queued it will say why in its log.

**Awaiting the warehouse** — the event went out and the warehouse has not
acknowledged it. It counts as acknowledged once `prd_shipmentStatus` carries a
CANCELLED event listing exactly this shipment's items. After 24 hours the
Reconcile cancellations workflow sends the event again and raises *Sends*.

**To cancel in Kibo** — the warehouse confirmed the cancellation and the Kibo
shipment is still open. The same workflow closes it on its next run, if the
shipment is in `READY`, `BACKORDER` or `CUSTOMER_CARE`.

**Done** — cancelled at the warehouse and in Kibo. Nothing further happens to
this row.
            """
        )


# This file is a page of streamlit_app.py, so it runs on import by st.navigation.
main()
