#!/usr/bin/env python3
"""
What every console page in this repository shares: the login, the GitHub
client, the CSV plumbing and the Logs and Folders tabs.

A page supplies only what is particular to it — which columns its CSV has and
what the entry form asks — and calls into here for the rest.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st

GITHUB_API = "https://api.github.com"

# The two-letter brand prefix that opens every shipmentOrderNumber and
# orderNumber decides the Kibo tenant. Kept in step with BRAND_TENANTS in the
# two workers in operational_tools/scripts. The consoles only use it to catch a
# typo before a record is queued; the worker is what actually resolves it.
BRAND_TENANTS = {
    "DG": "29916", "LN": "29917", "MO": "29988", "SC": "29989",
    "CO": "29990", "RC": "29990", "WL": "29991", "SW": "29992",
    "CA": "29993", "CV": "30014", "HB": "30014", "BA": "30014",
    "RL": "30016", "OW": "30016", "FA": "30017", "VW": "30017",
    "DJ": "30018", "AT": "30019", "LA": "30019", "PL": "30020",
    "CL": "30020", "HE": "30021", "FE": "30022", "CR": "30022",
    "FO": "30023", "EF": "30023", "PJ": "30475", "FU": "32145",
    "WF": "32147", "ZH": "32147", "VE": "32147", "BO": "33692",
    "AL": "33693", "MA": "33693", "PA": "33693", "FL": "33693",
    "BB": "33694", "TM": "54516",
}


def brand_prefix(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().upper()
        if len(text) >= 2 and text[:2].isalpha():
            return text[:2]
    return ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    token: str
    repo: str
    branch: str
    input_dir: str
    logs_dir: str
    archive_dir: str
    error_dir: str


def settings(app_key: str, defaults: Dict[str, str]) -> Settings:
    """
    The `[github]` section holds the token, the repository and the branch, which
    every page shares. Each page then has its own folders, taken from a section
    named after it (`[kibo]`, `[warehouse]`) and falling back to `[github]` and
    then to the defaults the page passes in. That is what keeps the two tools
    from reading and writing each other's queues.
    """
    section = dict(st.secrets.get("github", {}))
    missing = [key for key in ("token", "repo") if not section.get(key)]
    if missing:
        st.error(
            "The app is not configured yet: the `github` section of the secrets is missing "
            + ", ".join(f"`{key}`" for key in missing)
            + ". See .streamlit/secrets.toml.example."
        )
        st.stop()

    app_section = dict(st.secrets.get(app_key, {}))

    def folder(name: str) -> str:
        value = app_section.get(name) or section.get(name) or defaults[name]
        return str(value).strip("/")

    return Settings(
        token=section["token"],
        repo=app_section.get("repo") or section["repo"],
        branch=app_section.get("branch") or section.get("branch", "main"),
        input_dir=folder("input_dir"),
        logs_dir=folder("logs_dir"),
        archive_dir=folder("archive_dir"),
        error_dir=folder("error_dir"),
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def verify_password(password: str, stored: str) -> bool:
    """
    Check a password against 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'.

    A plain string with no '$' is treated as a plaintext password, which is only
    meant for a first local run; the README explains how to hash properly.
    """
    stored = str(stored)
    if "$" not in stored:
        return hmac.compare_digest(password, stored)
    try:
        algorithm, iterations, salt_hex, expected_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), expected_hex)


def _auth_sections() -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """
    Split the `[auth]` secrets into administrators and per-app users.

    A plain `name = "<hash>"` directly under `[auth]` is an administrator and
    reaches every page. A nested `[auth.<app_key>]` table holds the users of
    that one page, so the same username — `backoffice` — can exist for several
    apps with a different password each, and each of those passwords opens only
    its own page.
    """
    raw = dict(st.secrets.get("auth", {}))
    admins = {name: value for name, value in raw.items() if isinstance(value, str)}
    per_app = {
        app: dict(users) for app, users in raw.items() if not isinstance(users, str)
    }
    return admins, per_app


def _apps_for(username: str, password: str) -> List[str]:
    """
    The apps these credentials open: ["*"] for an administrator, a list of app
    keys for a per-app user, or [] when nothing matched.
    """
    admins, per_app = _auth_sections()
    stored = admins.get(username)
    if stored and verify_password(password, stored):
        return ["*"]
    return [
        app for app, users in sorted(per_app.items())
        if users.get(username) and verify_password(password, users[username])
    ]


def has_access(app_key: str) -> bool:
    apps = st.session_state.get("apps", [])
    return "*" in apps or app_key in apps


def is_admin() -> bool:
    return "*" in st.session_state.get("apps", [])


def accessible(app_keys: List[str]) -> List[str]:
    return [key for key in app_keys if has_access(key)]


def require_access(app_key: str, label: str) -> None:
    """Stop the page when the signed-in user is not allowed on it."""
    if has_access(app_key):
        return
    st.error(
        f"Your login does not have access to {label}. "
        "Ask an administrator to add you, then sign in again."
    )
    st.stop()


def login_gate(caption: str = "Operational tools.") -> str:
    """
    Render the login form and return the signed-in username.

    One sign-in covers every page the account is allowed on: the session is
    shared, so a page reached from the sidebar does not ask again.
    """
    if st.session_state.get("username"):
        return st.session_state["username"]

    admins, per_app = _auth_sections()
    if not admins and not per_app:
        st.error("No user is configured. Add an `[auth]` section to the secrets.")
        st.stop()

    st.markdown("### Sign in")
    st.caption(caption)
    with st.form("login"):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        apps = _apps_for(username.strip(), password)
        if apps:
            st.session_state["username"] = username.strip()
            st.session_state["apps"] = apps
            st.rerun()
        else:
            st.error("Wrong username or password.")
    st.stop()
    return ""  # unreachable


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class GitHub:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def put_file(self, path: str, content: bytes, message: str) -> Tuple[bool, str]:
        url = f"{GITHUB_API}/repos/{self.config.repo}/contents/{path}"
        body = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.config.branch,
        }
        try:
            response = self.session.put(url, json=body, timeout=30)
        except requests.RequestException as exc:
            return False, str(exc)
        if 200 <= response.status_code < 300:
            return True, ""
        return False, f"HTTP {response.status_code}: {response.text[:400]}"

    def list_dir(self, path: str) -> Tuple[List[Dict[str, Any]], str]:
        url = f"{GITHUB_API}/repos/{self.config.repo}/contents/{path}"
        try:
            response = self.session.get(url, params={"ref": self.config.branch}, timeout=30)
        except requests.RequestException as exc:
            return [], str(exc)
        if response.status_code == 404:
            return [], ""
        if not (200 <= response.status_code < 300):
            return [], f"HTTP {response.status_code}: {response.text[:400]}"
        try:
            payload = response.json()
        except ValueError:
            return [], "The directory listing was not JSON."
        if not isinstance(payload, list):
            return [], "That path is not a directory."
        return [entry for entry in payload if entry.get("type") == "file"], ""

    def read_file(self, path: str) -> Tuple[str, str]:
        url = f"{GITHUB_API}/repos/{self.config.repo}/contents/{path}"
        try:
            response = self.session.get(
                url,
                params={"ref": self.config.branch},
                headers={"Accept": "application/vnd.github.raw"},
                timeout=30,
            )
        except requests.RequestException as exc:
            return "", str(exc)
        if not (200 <= response.status_code < 300):
            return "", f"HTTP {response.status_code}: {response.text[:400]}"
        return response.text, ""


# ---------------------------------------------------------------------------
# CSV handling
# ---------------------------------------------------------------------------

def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def parse_uploaded_csv(
    raw: bytes,
    aliases: Dict[str, str],
    columns: List[str],
    required_any: List[str],
    required_message: str,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Return (rows, problems). Rows use the canonical column names.

    `required_any` lists the columns of which at least one has to be present in
    the header, and on each row.
    """
    problems: List[str] = []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        problems.append("The file is not UTF-8; it was read as Latin-1.")

    if not text.strip():
        return [], ["The file is empty."]

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return [], ["The file has no header row."]

    mapping: Dict[int, str] = {}
    unknown: List[str] = []
    for index, name in enumerate(header):
        canonical = aliases.get(normalize_header(name))
        if canonical:
            mapping.setdefault(index, canonical)
        elif str(name).strip():
            unknown.append(str(name).strip())

    if unknown:
        problems.append("Ignored column(s): " + ", ".join(unknown) + ".")

    if not (set(required_any) & set(mapping.values())):
        problems.append(
            required_message + " Found: " + ", ".join(str(name) for name in header) + "."
        )
        return [], problems

    rows: List[Dict[str, str]] = []
    for row_number, row in enumerate(reader, start=2):
        if not row or all(not str(cell).strip() for cell in row):
            continue
        record = {column: "" for column in columns}
        for index, canonical in mapping.items():
            if index < len(row) and canonical in record:
                record[canonical] = str(row[index]).strip()
        if not any(record.get(column) for column in required_any):
            problems.append(
                f"Row {row_number} is missing "
                + " or ".join(required_any)
                + "; the workflow will report it as failed."
            )
        rows.append(record)

    if not rows:
        problems.append("The file has a header but no data rows.")
    return rows, problems


def rows_to_csv(rows: List[Dict[str, str]], columns: List[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue().encode("utf-8")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug[:60] or "upload"


def build_filename(username: str, kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}__{safe_slug(username)}__{safe_slug(kind)}.csv"


def submit_rows(
    github: GitHub,
    config: Settings,
    rows: List[Dict[str, str]],
    columns: List[str],
    username: str,
    kind: str,
    subject: str,
) -> None:
    filename = build_filename(username, kind)
    path = f"{config.input_dir}/{filename}"
    ok, error = github.put_file(
        path,
        rows_to_csv(rows, columns),
        f"{subject}: {len(rows)} record(s) from {username}",
    )
    if ok:
        st.success(
            f"Queued {len(rows)} record(s) as `{filename}` in `{config.input_dir}/`. "
            "The workflow picks it up on its next run and writes a log."
        )
    else:
        st.error(f"The file could not be committed: {error}")


# ---------------------------------------------------------------------------
# Shared page furniture
# ---------------------------------------------------------------------------

def page_style() -> None:
    st.markdown(
        """
        <style>
          .block-container { max-width: 1040px; padding-top: 2.2rem; }
          div[data-testid="stMetricValue"] { font-size: 1.5rem; }
          pre { font-size: 0.78rem; line-height: 1.45; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def start_page(title: str, config: Optional[Settings] = None,
               caption: str = "", app_key: str = "", label: str = "") -> str:
    """
    Style, sign-in, access check and the header every console page opens with.

    Passing app_key makes the page refuse a user who is not allowed on it, even
    if they reach it by its URL rather than through the sidebar.
    """
    page_style()
    username = login_gate()
    if app_key:
        require_access(app_key, label or title)
    header_left, header_right = st.columns([4, 1])
    header_left.markdown(f"## {title}")
    if is_admin():
        header_right.caption("admin")
    if config is not None:
        header_left.caption(
            caption or f"{config.repo} · branch {config.branch} · {config.input_dir}/"
        )
    elif caption:
        header_left.caption(caption)
    if header_right.button("Sign out"):
        st.session_state.clear()
        st.rerun()
    return username


def tab_logs(github: GitHub, config: Settings) -> None:
    st.markdown("#### Run logs")

    if st.button("Refresh"):
        st.cache_data.clear()

    entries, error = github.list_dir(config.logs_dir)
    if error:
        st.error(f"The logs could not be listed: {error}")
        return

    logs = sorted(
        (entry for entry in entries if entry.get("name", "").endswith(".log")),
        key=lambda entry: entry.get("name", ""),
        reverse=True,
    )
    if not logs:
        st.info(
            f"No run log yet. The workflow writes one each time it finds a file in "
            f"{config.input_dir}/."
        )
        return

    choice = st.selectbox("Run", [entry["name"] for entry in logs[:50]])
    content, error = github.read_file(f"{config.logs_dir}/{choice}")
    if error:
        st.error(f"The log could not be read: {error}")
        return

    summary = re.search(r"^Records\s*:\s*(.+)$", content, flags=re.MULTILINE)
    if summary:
        st.caption(summary.group(1))
    st.code(content, language="text")
    st.download_button("Download this log", content, file_name=choice, mime="text/plain")


def tab_queue(github: GitHub, config: Settings) -> None:
    st.markdown("#### Folders")
    columns = st.columns(3)
    for column, (label, path) in zip(
        columns,
        [("Waiting", config.input_dir), ("Archive", config.archive_dir),
         ("Errors", config.error_dir)],
    ):
        entries, error = github.list_dir(path)
        files = [entry for entry in entries if not entry.get("name", "").startswith(".")]
        with column:
            st.metric(label, "—" if error else len(files))
            st.caption(f"`{path}/`")
            if error:
                st.caption(error)
            else:
                for entry in sorted(files, key=lambda item: item["name"], reverse=True)[:10]:
                    st.caption(entry["name"])
