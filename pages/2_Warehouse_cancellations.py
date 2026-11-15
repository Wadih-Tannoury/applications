#!/usr/bin/env python3
"""
Warehouse cancellation console.

Drops cancellation CSV files into the warehouse queue of the operational_tools
repository (warehouse/input/), where the Warehouse cancellations workflow picks
them up and sends a CANCEL event to the GXO endpoint, and reads the run logs
back out.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.console import (  # noqa: E402
    BRAND_TENANTS,
    GitHub,
    brand_prefix,
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

CSV_COLUMNS = ["shipmentOrderNumber", "productId", "orderNumber", "quantity",
               "reasonCode", "reasonMessage"]

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
    "reasonmessage": "reasonMessage",
    "moreinfo": "reasonMessage",
    "tenantid": "tenantId",
    "siteid": "siteId",
}

DEFAULT_REASON_MESSAGE = "Kibo notification cancellation"


def unknown_prefixes(rows: List[Dict[str, str]]) -> List[str]:
    """
    Brand prefixes the worker will not be able to turn into a tenant.

    Only rows that carry a productId need one: those are the rows the worker
    reads from Kibo before deciding where the cancellation goes. A
    whole-shipment row never touches Kibo.
    """
    found = set()
    for row in rows:
        if not row.get("productId") or row.get("tenantId"):
            continue
        prefix = brand_prefix(row.get("shipmentOrderNumber", ""), row.get("orderNumber", ""))
        if prefix and prefix not in BRAND_TENANTS:
            found.add(prefix)
        elif not prefix:
            found.add("(no letters)")
    return sorted(found)


def product_rule_note() -> None:
    st.info(
        "The warehouse event stops a **whole shipment**: it carries no products. So when a "
        "row names a productId, the worker reads the shipment from Kibo first:\n\n"
        "- that product is the **only** product on the shipment → the cancellation goes to "
        "the warehouse;\n"
        "- the shipment holds **other products too** → the warehouse is told nothing, and "
        "only that product is cancelled in Kibo, so the rest still ships.\n\n"
        "Either way the log says which of the two happened.",
        icon="📦",
    )


def tab_upload(github: GitHub, config, username: str) -> None:
    st.markdown("#### Upload a CSV")
    st.caption(
        "Columns: shipmentOrderNumber (required), productId, orderNumber, quantity, "
        "reasonCode, reasonMessage. An empty productId cancels the whole shipment outright."
    )
    product_rule_note()

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], label_visibility="collapsed")
    if uploaded is None:
        with st.expander("Example file"):
            st.code(
                "shipmentOrderNumber,productId,orderNumber\n"
                "OW-USA00037769,,\n"
                "OW-USA00037770,518055266024613,\n"
                "DJ-ITA00012345,,DJ-IT-00012345\n",
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

    preview = [
        dict(
            row,
            scope="depends on the product" if row.get("productId") else "whole shipment",
            tenant=(
                BRAND_TENANTS.get(
                    brand_prefix(row.get("shipmentOrderNumber", ""), row.get("orderNumber", "")),
                    "?",
                )
                if row.get("productId")
                else "—"
            ),
        )
        for row in rows
    ]
    st.dataframe(preview, use_container_width=True, hide_index=True)

    gated = sum(1 for row in rows if row.get("productId"))
    st.caption(
        f"{len(rows)} record(s) ready — {len(rows) - gated} cancel the whole shipment at the "
        f"warehouse, {gated} depend on what else is on the shipment."
    )

    unknown = unknown_prefixes(rows)
    if unknown:
        st.warning(
            "Unrecognised brand prefix on a row that carries a productId: "
            + ", ".join(unknown)
            + ". Those records will fail unless the prefix is added to the worker's tenant map."
        )

    if st.button("Queue these records", type="primary"):
        submit_rows(github, config, rows, CSV_COLUMNS, username,
                    uploaded.name.rsplit(".", 1)[0], "Warehouse cancellation request")


def tab_single(github: GitHub, config, username: str) -> None:
    st.markdown("#### Cancel one shipment at the warehouse")
    st.caption("The entry is turned into a one-row CSV and dropped in the input folder.")

    scope = st.radio(
        "What do you want to cancel?",
        ["The whole shipment", "One product (the worker decides where it lands)"],
        horizontal=False,
    )
    if scope != "The whole shipment":
        product_rule_note()

    with st.form("single"):
        shipment_order_number = st.text_input(
            "shipmentOrderNumber", placeholder="OW-USA00037769"
        )
        product_id = ""
        order_number = ""
        if scope != "The whole shipment":
            column_left, column_right = st.columns(2)
            product_id = column_left.text_input(
                "productId", placeholder="518055266024613", help="productCode, UPC or SKU"
            )
            order_number = column_right.text_input(
                "orderNumber (optional)",
                placeholder="OW-US-00037769",
                help="Used to read the shipment from Kibo. Left empty, the worker derives it "
                     "from the shipmentOrderNumber.",
            )
        reason_message = st.text_input("Reason message", value=DEFAULT_REASON_MESSAGE)
        submitted = st.form_submit_button("Queue this cancellation", type="primary")

    if not submitted:
        return

    shipment_order_number = shipment_order_number.strip()
    product_id = product_id.strip()
    order_number = order_number.strip()

    if not shipment_order_number:
        st.error("Enter a shipmentOrderNumber.")
        return
    if scope != "The whole shipment" and not product_id:
        st.error("Enter a productId, or switch to cancelling the whole shipment.")
        return

    if product_id:
        prefix = brand_prefix(shipment_order_number, order_number)
        if prefix not in BRAND_TENANTS:
            st.error(
                f"'{prefix or shipment_order_number}' is not a brand the tool knows. "
                "A row that names a product has to read the shipment from Kibo, and the "
                "two-letter brand code is what picks the tenant."
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
                "quantity": "",
                "reasonCode": "",
                "reasonMessage": reason_message.strip() or DEFAULT_REASON_MESSAGE,
            }
        ],
        CSV_COLUMNS,
        username,
        "product" if product_id else "shipment",
        "Warehouse cancellation request",
    )


def main() -> None:
    # set_page_config lives in streamlit_app.py: it runs once for the session,
    # and this file is one page of that app.
    config = settings(APP_KEY, DEFAULT_FOLDERS)
    username = start_page("Warehouse cancellation console", config, app_key=APP_KEY, label="Warehouse cancellations")
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
