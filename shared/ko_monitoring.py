#!/usr/bin/env python3
"""
What the KO monitoring page needs beyond `shared/console.py`.

Three things the other pages have no use for:

  * a commit holding **two** files — the pending-monitoring CSV and the IMKO
    workbook — in one go. The other consoles queue one small CSV through the
    contents endpoint; the workbook is several megabytes, which that endpoint
    will not take, and the pair has to arrive together or the workflow starts
    on half a batch. The git data API does both: blob, tree, commit, ref.
  * the state of the GitHub Actions runs, so the page can show what became of
    an upload without anyone opening GitHub.
  * the two settings the shared `Settings` dataclass has no field for: the
    output folder and the workflow file name.

Nothing here writes to BigQuery or calls the documents API; that is the
workflow's job, under the operational_tools secrets.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st

from shared.console import GitHub, Settings

GITHUB_API = "https://api.github.com"

APP_KEY = "ko_monitoring"

DEFAULT_FOLDERS = {
    "input_dir": "ko_monitoring/input",
    "logs_dir": "ko_monitoring/logs",
    "archive_dir": "ko_monitoring/archive",
    "error_dir": "ko_monitoring/error",
}

DEFAULT_OUTPUT_DIR = "ko_monitoring/output"
DEFAULT_WORKFLOW = "ko_monitoring.yml"


def extra(name: str, fallback: str) -> str:
    """A setting the shared Settings dataclass has no field for."""
    section = dict(st.secrets.get(APP_KEY, {}))
    return str(section.get(name) or fallback).strip("/")


def output_dir() -> str:
    return extra("output_dir", DEFAULT_OUTPUT_DIR)


def workflow() -> str:
    section = dict(st.secrets.get(APP_KEY, {}))
    return str(section.get("workflow") or DEFAULT_WORKFLOW).strip()


# ---------------------------------------------------------------------------
# One commit, several files
# ---------------------------------------------------------------------------

def _call(github: GitHub, method: str, path: str, **kwargs: Any) -> Tuple[Any, str]:
    url = f"{GITHUB_API}/repos/{github.config.repo}/{path.lstrip('/')}"
    try:
        response = github.session.request(method, url, timeout=60, **kwargs)
    except requests.RequestException as exc:
        return None, str(exc)
    if response.status_code == 404:
        return None, "404"
    if not (200 <= response.status_code < 300):
        return None, f"HTTP {response.status_code}: {response.text[:400]}"
    if not response.content:
        return {}, ""
    try:
        return response.json(), ""
    except ValueError:
        return response.text, ""


def commit_files(
    github: GitHub, files: List[Tuple[str, bytes]], message: str,
) -> Tuple[str, str]:
    """
    Commit several files in ONE commit. Returns (commit sha, error).

    One commit is one push event, so the workflow starts once, with the pair
    already complete — never on a lone CSV whose workbook is still uploading.
    """
    branch = github.config.branch

    reference, error = _call(github, "GET", f"git/ref/heads/{branch}")
    if error:
        return "", (f"branch `{branch}` was not found" if error == "404" else error)
    head = reference["object"]["sha"]

    commit, error = _call(github, "GET", f"git/commits/{head}")
    if error:
        return "", error
    base_tree = commit["tree"]["sha"]

    entries = []
    for path, content in files:
        blob, error = _call(github, "POST", "git/blobs", json={
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        if error:
            return "", f"{path}: {error}"
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree, error = _call(github, "POST", "git/trees", json={
        "base_tree": base_tree, "tree": entries,
    })
    if error:
        return "", error

    new_commit, error = _call(github, "POST", "git/commits", json={
        "message": message, "tree": tree["sha"], "parents": [head],
    })
    if error:
        return "", error

    _, error = _call(github, "PATCH", f"git/refs/heads/{branch}", json={
        "sha": new_commit["sha"], "force": False,
    })
    if error:
        return "", error + " — someone else pushed while this was uploading. Try again."
    return new_commit["sha"], ""


def read_bytes(github: GitHub, path: str) -> Tuple[bytes, str]:
    """The raw bytes of a file, whatever its size."""
    url = f"{GITHUB_API}/repos/{github.config.repo}/contents/{path}"
    try:
        response = github.session.get(
            url,
            params={"ref": github.config.branch},
            headers={"Accept": "application/vnd.github.raw"},
            timeout=60,
        )
    except requests.RequestException as exc:
        return b"", str(exc)
    if response.status_code == 404:
        return b"", "404"
    if not (200 <= response.status_code < 300):
        return b"", f"HTTP {response.status_code}: {response.text[:400]}"
    return response.content, ""


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------

def runs(github: GitHub, limit: int = 10) -> Tuple[List[Dict[str, Any]], str]:
    """The recent runs of the KO monitoring workflow, newest first."""
    payload, error = _call(
        github, "GET", f"actions/workflows/{workflow()}/runs",
        params={"per_page": limit, "branch": github.config.branch},
    )
    if error == "404":
        return [], (
            f"`{workflow()}` is not in the repository yet, or the token cannot read "
            "Actions. Everything else on this page still works."
        )
    if error:
        return [], error
    return list(payload.get("workflow_runs", [])), ""


def dispatch(github: GitHub, inputs: Optional[Dict[str, str]] = None) -> str:
    """Start the workflow by hand. Returns an error, or '' on success."""
    _, error = _call(
        github, "POST", f"actions/workflows/{workflow()}/dispatches",
        json={"ref": github.config.branch, "inputs": inputs or {}},
    )
    if error == "404":
        return (
            f"`{workflow()}` was not found, or the token has no "
            "`Actions: Read and write` permission."
        )
    return error


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def show_time(value: Any) -> str:
    """An ISO timestamp as the page shows it: minutes, UTC."""
    if not value:
        return ""
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def decode_csv(raw: bytes) -> Any:
    """The run CSV as a DataFrame, read as text whatever the encoding."""
    import io

    import pandas as pd
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
