#!/usr/bin/env python3
"""
Operational applications — the front door.

One sign-in, one deployment, a page per tool. Each page queues a CSV in its own
folder of the operational_tools repository and reads that tool's run logs back
out; the GitHub Actions workflows there do the actual work.

Who sees what comes from the `[auth]` secrets: an administrator reaches every
page, a per-app user reaches only their own. The sidebar is built from that, so
a user never sees a tool they cannot open, and the page itself refuses them
anyway if they reach it by its URL.

Adding a tool means adding a file under pages/, a line in TOOLS below, and a
folder section in the secrets.
"""
from __future__ import annotations

import streamlit as st

from shared.console import accessible, is_admin, login_gate, page_style

TOOLS = [
    {
        "key": "kibo",
        "page": "pages/1_Kibo_cancellations.py",
        "label": "Kibo cancellations",
        "icon": "📦",
        "blurb": "Cancel an item, a shipment or a whole order in Kibo. "
                 "Queues into `input/`; the Cancellations workflow applies it.",
    },
    {
        "key": "warehouse",
        "page": "pages/2_Warehouse_cancellations.py",
        "label": "Warehouse cancellations",
        "icon": "📦",
        "blurb": "Stop a shipment at the GXO warehouse. Name a product and the worker "
                 "sends the event only if that product is the whole shipment, otherwise "
                 "it cancels just that item in Kibo. Queues into `warehouse/input/`.",
    },
]


def home() -> None:
    page_style()
    username = st.session_state.get("username", "")

    header_left, header_right = st.columns([4, 1])
    header_left.markdown("## Operational applications")
    header_left.caption(
        f"Signed in as {username}" + (" · administrator" if is_admin() else "")
    )
    if header_right.button("Sign out"):
        st.session_state.clear()
        st.rerun()

    allowed = set(accessible([tool["key"] for tool in TOOLS]))

    st.write("")
    for tool in TOOLS:
        if tool["key"] not in allowed:
            continue
        with st.container(border=True):
            st.page_link(tool["page"], label=f"**{tool['label']}**", icon=tool["icon"])
            st.caption(tool["blurb"])

    if not allowed:
        st.warning(
            "This login does not have access to any tool yet. Ask an administrator "
            "to add it to one."
        )
        return

    st.write("")
    st.caption(
        "Every tool writes to its own queue folder, so two runs never touch the same "
        "files. What a run did is in that tool's Logs tab."
    )


def main() -> None:
    st.set_page_config(page_title="Operational applications", page_icon="📦", layout="centered")
    page_style()

    login_gate("Operational applications.")

    pages = [st.Page(home, title="Home", icon="🏠", default=True)]
    allowed = set(accessible([tool["key"] for tool in TOOLS]))
    pages += [
        st.Page(tool["page"], title=tool["label"], icon=tool["icon"])
        for tool in TOOLS
        if tool["key"] in allowed
    ]

    st.navigation(pages).run()


if __name__ == "__main__":
    main()
