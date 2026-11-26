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
and been marked `ko_resolved`.

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
    ("shipment_order_number", "shipment_order_number"),
    ("oerodp", "oerodp"),
    ("ub_imko", "ub_imko"),
    ("logistic_order_number", "logistic_order_number"),
    ("oms_shipment_number", "oms_shipment_number"),
    ("order_submit_date", "order_submit_date"),
    ("shipment_creation_date", "shipment_creation_date"),
    ("action", "action"),
    ("initial_fulfillment_status", "initial_fulfillment_status"),
    ("updated_fulfillment_status", "updated_fulfillment_status"),
    ("location_provider", "location_provider"),
    ("location_name", "location_name"),
    ("ko_fixed", "ko_fixed"),
    ("ko_fix_date", "ko_fix_date"),
    ("last_tried_by_gxo_date", "last_tried_by_gxo_date"),
    ("gxo_result_code", "gxo_result_code"),
    ("tried_by_gxo_after_the_fix", "tried_by_gxo_after_the_fix"),
    ("ko_reason", "ko_reason"),
    ("ko_resolved", "ko_resolved"),
    ("ko_resolved_date", "ko_resolved_date"),
    ("source", "source"),
    ("brand", "brand"),
    ("channel", "channel"),
    # One column per place a KO reason can come from. Named for the source,
    # never for what it means: ko_reason is still the single working answer,
    # and these are what it was chosen from.
    ("mon_ko_source", "mon_ko_source"),
    ("mon_blocking_reason", "mon_blocking_reason"),
    ("mon_external_error_message", "mon_external_error_message"),
    ("mon_error_message", "mon_error_message"),
    ("b2c_action", "b2c_action"),
    ("gxo_ko_reason", "gxo_ko_reason"),
    ("docs_reason", "docs_reason"),
    ("action_needed_from_gxo", "action_needed_from_gxo"),
    ("in_latest_gxo_input", "in_latest_gxo_input"),
    ("eligible_for_package_closure", "eligible_for_package_closure"),
    ("shipping_tracking_number", "shipping_tracking_number"),
    ("return_tracking_number", "return_tracking_number"),
    ("api_call_date", "api_call_date"),
    ("batch", "batch"),
    # Bookkeeping, shown last.
    ("last_run", "last_run"),
    ("row_insertion_date", "first_seen"),
    ("row_last_modified_date", "last_updated"),
]

BOOL_COLUMNS = {
    "ko_fixed", "ko_resolved", "tried_by_gxo_after_the_fix",
    "in_latest_gxo_input",
    "eligible_for_package_closure",
}

TIMESTAMP_COLUMNS = {
    "ko_fix_date", "last_tried_by_gxo_date", "ko_resolved_date",
    "api_call_date", "first_seen", "last_updated",
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
