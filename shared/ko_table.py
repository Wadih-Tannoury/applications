#!/usr/bin/env python3
"""
Reading the KO operational table.

    `tlg-business-intelligence-prd`.til.ko_operational_table

is written by the KO monitoring workflow in operational_tools: one row per
shipment, kept between runs. This console only ever reads it.

It is the dashboard's first source, and `ko_monitoring/output/latest.csv` in
the repository is the fallback for a deployment with no `[bigquery]` secrets.
The difference between the two is memory: the CSV is the last run, the table is
every run — including the shipments that have since dropped out of the uploads
and been marked `gxo_resolved`.

The read is uncached here; the page wraps it in a short cache so moving a
filter does not re-query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:  # reported on the page, which stays up
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]

PROJECT = "tlg-business-intelligence-prd"
DATASET = "til"
TABLE = "ko_operational_table"
FQN = f"{PROJECT}.{DATASET}.{TABLE}"

BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

# (BigQuery column, the heading the dashboard shows it under). The order is the
# order of the table on the page, and the first block is what was asked for.
COLUMNS: List[Tuple[str, str]] = [
    ("shipment_order_number", "shipmentOrderNumber"),
    ("oerodp", "OERODP"),
    ("ub_imko", "ub_imko"),
    ("logistic_order", "Logistic order"),
    ("oms_shipment_number", "OMS shipment #"),
    ("order_date", "Order date"),
    ("shipment_created", "Shipment created"),
    ("shipment_created_effective", "Shipment created (effective)"),
    ("action", "Action"),
    ("active_task", "Active task"),
    ("gxo_ko_reason", "GXO KO reason"),
    ("ko_fixed", "ko_fixed"),
    ("ko_fix_date", "ko_fix_date"),
    ("last_tried_by_gxo_date", "last_tried_by_gxo_date"),
    ("get_documents_error", "get_documents_error"),
    ("gxo_resolved", "gxo_resolved"),
    ("gxo_resolved_date", "gxo_resolved_date"),
    # The evidence, hidden until it is asked for.
    ("ko_status", "ko_status"),
    ("source", "source"),
    ("brand", "brand"),
    ("eligible_for_package_closure", "eligibleForPackageClosure"),
    ("label_shplbl_populated", "label_SHPLBL_populated"),
    ("label_shplbl_format", "label_SHPLBL_format"),
    ("label_retlbl_present", "label_RETLBL_present"),
    ("label_retlbl_populated", "label_RETLBL_populated"),
    ("label_retlbl_format", "label_RETLBL_format"),
    ("shipping_tracking_number", "shipping_tracking_number"),
    ("return_tracking_number", "return_tracking_number"),
    ("doc_inv_populated", "doc_INV_populated"),
    ("doc_rpi_populated", "doc_RPI_populated"),
    ("api_http_status", "api_http_status"),
    ("api_message", "api_message"),
    ("api_create_error", "api_create_error"),
    ("api_called_at", "api_called_at"),
    ("gxo_result_code", "gxo_result_code"),
    ("batch", "batch"),
    ("times_seen", "times_seen"),
    ("last_run", "last_run"),
    ("row_insertion_date", "First seen"),
    ("row_last_modified_date", "Last updated"),
]

BOOL_COLUMNS = {
    "ko_fixed", "gxo_resolved", "eligibleForPackageClosure",
    "label_SHPLBL_populated", "label_RETLBL_present",
    "label_RETLBL_populated",
    "doc_INV_populated", "doc_RPI_populated",
}

TIMESTAMP_COLUMNS = {
    "ko_fix_date", "last_tried_by_gxo_date", "gxo_resolved_date",
    "api_called_at", "First seen", "Last updated",
}


@dataclass
class Read:
    """One read of the table."""
    frame: Optional[pd.DataFrame]
    read_at: datetime
    source: str = FQN
    error: str = ""
    missing_columns: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.frame is not None


def configured() -> bool:
    """Is there a `[bigquery]` section at all?"""
    return bool(dict(st.secrets.get("bigquery", {})))


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
            "The app has no `[bigquery]` section in its secrets, so it reads the "
            "last run's CSV from the repository instead of the table. See "
            ".streamlit/secrets.toml.example."
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


def available_columns(bq: Any) -> Tuple[List[str], str]:
    """The columns the table actually has, lower-cased."""
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


def fetch(limit: int = 20000) -> Read:
    """
    The whole table, resolved rows included; the page filters from there.

    A column the table does not have is selected as NULL rather than failing
    the read, so a deployment that has not run the newest
    `sql/ko_operational_table.sql` still gets its dashboard and a note saying
    which columns are missing.
    """
    now = datetime.now(timezone.utc)
    bq, error = client()
    if error:
        return Read(frame=None, read_at=now, error=error)

    present, error = available_columns(bq)
    if error:
        return Read(frame=None, read_at=now, error=error)
    if not present:
        return Read(frame=None, read_at=now, error=(
            f"`{FQN}` does not exist, or this service account cannot see it. Run "
            "`sql/ko_operational_table.sql` in the operational_tools repository."
        ))

    missing = [column for column, _ in COLUMNS if column not in present]
    # Selected under their real names, never aliased to the headings: a
    # BigQuery field name takes only letters, digits and underscores, so
    # `OMS shipment #` and `Shipment created (effective)` are rejected outright
    # as aliases. The renaming to headings happens below, in pandas.
    selects = [
        (f"`{column}`" if column in present
         else f"CAST(NULL AS STRING) AS `{column}`")
        for column, _ in COLUMNS
    ]
    order = ("row_last_modified_date" if "row_last_modified_date" in present
             else "shipment_order_number")

    sql = f"""
        SELECT {', '.join(selects)}
        FROM `{FQN}`
        ORDER BY `{order}` DESC
        LIMIT {int(limit)}
    """
    try:
        # Built row by row rather than with to_dataframe(): that path wants
        # pyarrow and db-dtypes for the DATE columns, and this table is a few
        # thousand rows, so the plain dicts cost nothing and the deployment
        # stays two packages lighter.
        rows = [dict(row) for row in bq.query(sql).result()]
    except Exception as exc:  # noqa: BLE001
        return Read(frame=None, read_at=now, error=f"The table could not be read: {exc}")

    frame = pd.DataFrame(rows, columns=[column for column, _ in COLUMNS])
    frame = frame.rename(columns={column: heading for column, heading in COLUMNS})

    return Read(frame=normalise(frame), read_at=now, missing_columns=missing)


def normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """
    The table as the page wants it: strings throughout, booleans as yes/no,
    timestamps to the minute.

    Doing it here rather than in the page means the table view, the filters and
    the CSV download can never show a value three different ways.
    """
    shown = pd.DataFrame(index=frame.index)
    for column in frame.columns:
        values = frame[column]
        if column in BOOL_COLUMNS:
            shown[column] = values.map(
                lambda value: "" if value is None or pd.isna(value)
                else ("yes" if bool(value) else "no")
            )
        elif column in TIMESTAMP_COLUMNS:
            moments = pd.to_datetime(values, errors="coerce", utc=True)
            shown[column] = moments.dt.strftime("%Y-%m-%d %H:%M").fillna("")
        else:
            shown[column] = values.map(
                lambda value: "" if value is None or (
                    not isinstance(value, str) and pd.isna(value)
                ) else str(value)
            )
    return shown
