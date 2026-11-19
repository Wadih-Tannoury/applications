#!/usr/bin/env python3
"""
Reading the warehouse cancellation ledger.

    `tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table

is written by the operational_tools repository: a row per shipment cancelled at
the GXO warehouse, and what became of it afterwards. Nothing here writes to it.
The console only ever reads, so a mistake on this page cannot cost a shipment.

The read is deliberately uncached. Streamlit re-runs the page script on every
sign-in, every refresh and every widget change, so the table is queried afresh
each time and what the page shows is what BigQuery holds at that moment. That is
a query per page view, which is what a ledger view is worth; if that ever
becomes too many, `fetch_rows` is the one place to put a `ttl` on.

Credentials come from the `[bigquery]` section of the Streamlit secrets: the
service account JSON, pasted field by field. It needs nothing more than
`roles/bigquery.dataViewer` on the dataset and `jobUser` on the project — give
it no write role, so the console cannot alter the ledger even by accident.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:  # reported on the page, which stays up
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


PROJECT = "tlg-business-intelligence-prd"
DATASET = "til"
TABLE = "warehouse_orders_cancellation_table"

FQN = f"{PROJECT}.{DATASET}.{TABLE}"

BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

# The columns the page shows, in the order it shows them, with the heading each
# one gets. Keeping the list here rather than in the page means the CSV download
# and the table can never drift apart.
COLUMNS: List[Tuple[str, str]] = [
    ("shipment_order_number", "Shipment"),
    ("shipment_items", "Items"),
    ("cancellation_sent_to_warehouse", "Sent"),
    ("cancellation_sent_to_warehouse_date", "Sent at"),
    ("cancellation_confirmed_by_warehouse", "Confirmed"),
    ("cancellation_confirmed_by_warehouse_date", "Confirmed at"),
    ("number_of_cancellations_sent_to_warehouse", "Sends"),
    ("cancelled_on_kibo", "Cancelled in Kibo"),
    ("cancelled_on_kibo_date", "Cancelled in Kibo at"),
    ("row_insertion_date", "Row created"),
    ("row_last_modified_date", "Row updated"),
]

BOOL_COLUMNS = {
    "cancellation_sent_to_warehouse",
    "cancellation_confirmed_by_warehouse",
    "cancelled_on_kibo",
}

TIMESTAMP_COLUMNS = {
    "cancellation_sent_to_warehouse_date",
    "cancellation_confirmed_by_warehouse_date",
    "cancelled_on_kibo_date",
    "row_insertion_date",
    "row_last_modified_date",
}

# A flag column written as a string by an older deployment still reads as a
# boolean here, the same way the workers read it.
TRUTHY = ("TRUE", "T", "YES", "Y", "1")


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in TRUTHY


@dataclass
class Ledger:
    """One read of the table."""
    rows: List[Dict[str, Any]]
    read_at: datetime
    error: str = ""
    missing_columns: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.missing_columns is None:
            self.missing_columns = []

    @property
    def ok(self) -> bool:
        return not self.error


def client() -> Tuple[Any, str]:
    """(client, error). The error is shown on the page; nothing raises."""
    if bigquery is None:
        return None, (
            "The `google-cloud-bigquery` package is not installed. Add it to "
            "requirements.txt and redeploy."
        )
    section = dict(st.secrets.get("bigquery", {}))
    if not section:
        return None, (
            "The app has no `[bigquery]` section in its secrets, so it cannot read "
            "the ledger. See .streamlit/secrets.toml.example."
        )
    missing = [
        key for key in ("type", "project_id", "private_key", "client_email")
        if not section.get(key)
    ]
    if missing:
        return None, (
            "The `[bigquery]` secrets section is missing "
            + ", ".join(f"`{key}`" for key in missing) + "."
        )
    try:
        credentials = service_account.Credentials.from_service_account_info(
            dict(section), scopes=BQ_SCOPES
        )
        return bigquery.Client(project=PROJECT, credentials=credentials), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not sign in to BigQuery: {exc}"


def table_columns(bq: Any) -> Tuple[List[str], str]:
    """The column names the table actually has, lower-cased."""
    sql = f"""
        SELECT column_name
        FROM `{PROJECT}.{DATASET}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = @table
    """
    try:
        job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("table", "STRING", TABLE),
        ]))
        return [str(row["column_name"]).lower() for row in job.result()], ""
    except Exception as exc:  # noqa: BLE001
        return [], f"Could not read the table's schema: {exc}"


def fetch_rows(days: int = 0, search: str = "", limit: int = 2000) -> Ledger:
    """
    Read the ledger.

    Called on every page run, so every sign-in and every refresh shows the table
    as it stands right now. Filtering happens in BigQuery rather than in the
    page, so a long ledger never has to be shipped here in full.

    days   0 reads everything, otherwise only rows touched in that window
    search matches a shipment number or an item code, case-insensitively
    limit  a ceiling so a runaway table cannot hang the page
    """
    now = datetime.now(timezone.utc)
    bq, error = client()
    if error:
        return Ledger(rows=[], read_at=now, error=error)

    available, error = table_columns(bq)
    if error:
        return Ledger(rows=[], read_at=now, error=error)
    if not available:
        return Ledger(rows=[], read_at=now, error=(
            f"`{FQN}` does not exist, or this service account cannot see it. Run "
            "`sql/warehouse_orders_cancellation_table.sql` in the operational_tools "
            "repository."
        ))

    wanted = [name for name, _ in COLUMNS]
    missing = [name for name in wanted if name not in available]
    # A column the table does not have is selected as NULL, so the page keeps its
    # shape and says which ones are absent rather than failing outright.
    selects = [
        (f"`{name}`" if name in available else f"CAST(NULL AS STRING) AS `{name}`")
        for name in wanted
    ]

    where: List[str] = []
    parameters: List[Any] = []
    if days > 0 and "row_last_modified_date" in available:
        where.append("`row_last_modified_date` >= @since")
        parameters.append(bigquery.ScalarQueryParameter(
            "since", "TIMESTAMP", now - timedelta(days=days)
        ))
    if search.strip():
        where.append(
            "(UPPER(CAST(`shipment_order_number` AS STRING)) LIKE @needle"
            " OR UPPER(CAST(`shipment_items` AS STRING)) LIKE @needle)"
        )
        parameters.append(bigquery.ScalarQueryParameter(
            "needle", "STRING", f"%{search.strip().upper()}%"
        ))

    order = ("`row_last_modified_date` DESC" if "row_last_modified_date" in available
             else "`shipment_order_number`")
    sql = f"""
        SELECT {", ".join(selects)}
        FROM `{FQN}`
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY {order}
        LIMIT @limit
    """
    parameters.append(bigquery.ScalarQueryParameter("limit", "INT64", max(1, limit)))

    try:
        job = bq.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=parameters
        ))
        rows = [dict(row.items()) for row in job.result()]
    except Exception as exc:  # noqa: BLE001
        return Ledger(rows=[], read_at=now, error=f"Could not read `{FQN}`: {exc}")

    for row in rows:
        for name in BOOL_COLUMNS:
            if name in row:
                row[name] = truthy(row[name])
    return Ledger(rows=rows, read_at=datetime.now(timezone.utc),
                  missing_columns=missing)


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """The counts the page puts above the table."""
    sent = sum(1 for row in rows if row.get("cancellation_sent_to_warehouse"))
    confirmed = sum(1 for row in rows if row.get("cancellation_confirmed_by_warehouse"))
    in_kibo = sum(1 for row in rows if row.get("cancelled_on_kibo"))
    return {
        "rows": len(rows),
        "sent": sent,
        "confirmed": confirmed,
        "awaiting": sent - confirmed,
        "cancelled_in_kibo": in_kibo,
        "to_cancel_in_kibo": confirmed - in_kibo,
    }


def stage_of(row: Dict[str, Any]) -> str:
    """
    Where one shipment has got to, in one word.

    Read in the order the stages actually happen, so a row only ever reports the
    furthest point it has reached.
    """
    if row.get("cancelled_on_kibo"):
        return "Done"
    if row.get("cancellation_confirmed_by_warehouse"):
        return "To cancel in Kibo"
    if row.get("cancellation_sent_to_warehouse"):
        return "Awaiting the warehouse"
    return "Not sent"


def overdue(row: Dict[str, Any], hours: float = 24.0) -> bool:
    """
    True when a cancellation has gone unconfirmed past the resend threshold.

    The same window the reconciliation workflow uses, so what the page marks is
    what that workflow will pick up on its next run.
    """
    if row.get("cancellation_confirmed_by_warehouse"):
        return False
    if not row.get("cancellation_sent_to_warehouse"):
        return False
    sent_at = row.get("cancellation_sent_to_warehouse_date")
    if not isinstance(sent_at, datetime):
        return False
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at <= datetime.now(timezone.utc) - timedelta(hours=hours)
