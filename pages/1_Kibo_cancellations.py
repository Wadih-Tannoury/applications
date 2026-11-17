#!/usr/bin/env python3
"""
Kibo cancellation console.

Drops cancellation CSV files into the Kibo queue of the operational_tools
repository (input/), where the Cancellations workflow picks them up, and reads
the run logs back out.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.console import (  # noqa: E402
    BRAND_TENANTS,
    REASON_CODES,
    GitHub,
    brand_prefix,
    parse_uploaded_csv,
    settings,
    start_page,
    submit_rows,
    tab_logs,
    tab_queue,
)

APP_KEY = "kibo"
DEFAULT_FOLDERS = {
    "input_dir": "input",
    "logs_dir": "logs",
    "archive_dir": "archive",
    "error_dir": "error",
}

CSV_COLUMNS = ["shipmentOrderNumber", "productId", "orderNumber", "quantity",
               "reasonCode", "moreInfo"]

# Columns every row has to carry. The cancellation reason is recorded against
# the order in Kibo, so a row without one is refused here rather than sent with
# a filler nobody chose.
REQUIRED = {"reasonCode": "reasonCode", "moreInfo": "moreInfo"}

ALIASES = {
    "shipmentordernumber": "shipmentOrderNumber",
    "son": "shipmentOrderNumber",
    "productid": "productId",
    "productcode": "productId",
    "sku": "productId",
    "upc": "productId",
    "ordernumber": "orderNumber",
    "externalorderid": "orderNumber",
    "externalordernumber": "orderNumber",
    "quantity": "quantity",
    "qty": "quantity",
    "reasoncode": "reasonCode",
    "reason": "reasonCode",
    "moreinfo": "moreInfo",
    "tenantid": "tenantId",
    "siteid": "siteId",
}

def missing_required(rows: List[Dict[str, str]]) -> List[str]:
    """Complaints about rows that cannot be sent as they stand."""
    problems = []
    for position, row in enumerate(rows, start=1):
        absent = [label for column, label in REQUIRED.items() if not row.get(column)]
        if not (row.get("shipmentOrderNumber") or row.get("orderNumber")):
            absent.insert(0, "shipmentOrderNumber or orderNumber")
        if absent:
            label = (row.get("shipmentOrderNumber") or row.get("orderNumber")
                     or "no order number")
            problems.append(
                f"Record {position} ({label}) is missing " + " and ".join(absent) + "."
            )
    return problems


def unknown_prefixes(rows: List[Dict[str, str]]) -> List[str]:
    """Brand prefixes the worker will not be able to turn into a tenant."""
    found = set()
    for row in rows:
        if row.get("tenantId"):
            continue
        prefix = brand_prefix(row.get("shipmentOrderNumber", ""), row.get("orderNumber", ""))
        if prefix and prefix not in BRAND_TENANTS:
            found.add(prefix)
        elif not prefix and (row.get("shipmentOrderNumber") or row.get("orderNumber")):
            found.add("(no letters)")
    return sorted(found)


def tab_upload(github: GitHub, config, username: str) -> None:
    st.markdown("#### Upload a CSV")
    st.caption(
        "Columns: shipmentOrderNumber, productId, orderNumber, quantity, and "
        "reasonCode and moreInfo (both required — they are recorded on the cancellation "
        "in Kibo). A productId cancels that item only; an empty productId cancels the "
        "whole shipment; a row with only an orderNumber cancels every shipment of that "
        "order."
    )

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], label_visibility="collapsed")
    if uploaded is None:
        with st.expander("Example file"):
            st.code(
                "shipmentOrderNumber,productId,orderNumber,reasonCode,moreInfo\n"
                "OW-USA00037769,518055266024613,,CustomerChangedTheirMind,Customer called\n"
                "OW-USA00037770,,,No Inventory,Nothing left in Milan\n"
                ",,OW-US-00037771,Fraud,Flagged by payments\n",
                language="text",
            )
        return

    rows, problems = parse_uploaded_csv(
        uploaded.getvalue(),
        ALIASES,
        CSV_COLUMNS,
        ["shipmentOrderNumber", "orderNumber"],
        "The header needs a shipmentOrderNumber or an orderNumber column.",
    )
    for problem in problems:
        st.warning(problem)
    if not rows:
        return

    preview = [
        dict(row, tenant=BRAND_TENANTS.get(
            brand_prefix(row.get("shipmentOrderNumber", ""), row.get("orderNumber", "")), "?"
        ))
        for row in rows
    ]
    st.dataframe(preview, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} record(s) ready.")

    unknown = unknown_prefixes(rows)
    if unknown:
        st.warning(
            "Unrecognised brand prefix: " + ", ".join(unknown) + ". "
            "Those records will fail unless the prefix is added to the worker's tenant map, "
            "or the row carries a tenantId."
        )

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
                    uploaded.name.rsplit(".", 1)[0], "Cancellation request")


def tab_single(github: GitHub, config, username: str) -> None:
    st.markdown("#### Cancel one item, shipment or order")
    st.caption("The entry is turned into a one-row CSV and dropped in the input folder.")

    scope = st.radio(
        "What do you want to cancel?",
        ["A single item", "A whole shipment", "A whole order"],
        horizontal=True,
    )

    with st.form("single"):
        shipment_order_number = ""
        product_id = ""
        order_number = ""
        quantity = ""

        if scope == "A single item":
            column_left, column_right = st.columns(2)
            shipment_order_number = column_left.text_input(
                "shipmentOrderNumber", placeholder="OW-USA00037769"
            )
            product_id = column_right.text_input(
                "productId", placeholder="518055266024613", help="productCode, UPC or SKU"
            )
            quantity = st.text_input(
                "Quantity", placeholder="leave empty to cancel the whole line"
            )
        elif scope == "A whole shipment":
            shipment_order_number = st.text_input(
                "shipmentOrderNumber", placeholder="OW-USA00037769"
            )
        else:
            order_number = st.text_input("orderNumber", placeholder="OW-US-00037769")

        column_left, column_right = st.columns(2)
        reason_code = column_left.selectbox("Reason code (required)", REASON_CODES)
        more_info = column_right.text_input(
            "More info (required)",
            placeholder="why this is being cancelled",
            help="Recorded on the cancellation in Kibo.",
        )

        submitted = st.form_submit_button("Submit Cancellation", type="primary")

    if not submitted:
        return

    shipment_order_number = shipment_order_number.strip()
    product_id = product_id.strip()
    order_number = order_number.strip()
    quantity = quantity.strip()

    if scope == "A single item" and not (shipment_order_number and product_id):
        st.error("A single item needs both a shipmentOrderNumber and a productId.")
        return
    if scope == "A whole shipment" and not shipment_order_number:
        st.error("Enter a shipmentOrderNumber.")
        return
    if scope == "A whole order" and not order_number:
        st.error("Enter an orderNumber.")
        return
    if quantity and not quantity.isdigit():
        st.error("The quantity has to be a whole number.")
        return
    if not more_info.strip():
        st.error("Enter the more info text: it is recorded on the cancellation in Kibo.")
        return

    prefix = brand_prefix(shipment_order_number, order_number)
    if prefix not in BRAND_TENANTS:
        st.error(
            f"'{prefix or shipment_order_number or order_number}' is not a brand the tool knows. "
            "Every number starts with a two-letter brand code such as OW or DJ, which is what "
            "picks the Kibo tenant."
        )
        return
    st.caption(f"Brand {prefix} · Kibo tenant {BRAND_TENANTS[prefix]}")

    submit_rows(
        github,
        config,
        [
            {
                "shipmentOrderNumber": shipment_order_number,
                "productId": product_id,
                "orderNumber": order_number,
                "quantity": quantity,
                "reasonCode": reason_code,
                "moreInfo": more_info.strip(),
            }
        ],
        CSV_COLUMNS,
        username,
        {"A single item": "item", "A whole shipment": "shipment", "A whole order": "order"}[scope],
        "Cancellation request",
    )


def main() -> None:
    # set_page_config lives in streamlit_app.py: it runs once for the session,
    # and this file is one page of that app.
    config = settings(APP_KEY, DEFAULT_FOLDERS)
    username = start_page("Kibo cancellation console", config, app_key=APP_KEY, label="Kibo cancellations")
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
