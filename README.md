# Applications

Every operational console lives here: one Streamlit deployment, one sign-in, a
page per tool. The pages hold no secrets and no data. They turn an upload or a
form entry into a CSV, commit it to the matching queue folder in the private
**operational_tools** repository, and read that tool's run logs back out. The
GitHub Actions workflows over there do the actual work.

```
  browser                     this repo                operational_tools (private)
 ┌──────────────────┐        ┌────────────────┐       ┌──────────────────────────┐
 │ Operational      │        │ streamlit_app  │       │ input/            ─▶ Kibo│
 │ applications     │ ─────▶ │ pages/         │ ────▶ │ warehouse/input/  ─▶ GXO │
 │  · Kibo          │        │ shared/        │  API  │ logs/ · warehouse/logs/  │
 │  · Warehouse     │ ◀───── │                │ ◀──── │ archive/ · error/        │
 └──────────────────┘        └────────────────┘       └──────────────────────────┘
```

## The pages

| Page | What it queues | Where |
|---|---|---|
| **Kibo cancellations** | An item, a shipment or a whole order to cancel in Kibo | `input/` |
| **Warehouse cancellations** | A shipment to stop at the GXO warehouse. With a productId, the worker sends the event only if that product is the whole shipment; otherwise it cancels just that item in Kibo and tells the warehouse nothing | `warehouse/input/` |

Each page has the same four tabs: **Upload CSV**, **Single cancellation**,
**Logs** and **Folders**. The Logs and Folders tabs only ever look at that
tool's own folders, so the two queues never show each other's files.

## Who can open what

Two kinds of login, both in the `[auth]` secrets:

| Kind | Where it goes in the secrets | Reaches |
|---|---|---|
| **Administrator** — `Wadih` | directly under `[auth]` | every page, present and future |
| **Per-app user** — `backoffice` | under `[auth.kibo]`, `[auth.warehouse]`, … | that one page |

The same username exists once per app with a different password each, so
`backoffice` + the Kibo password opens the Kibo page, and `backoffice` + the
warehouse password opens the warehouse page. Neither sees the other.

```toml
[auth]
Wadih = "pbkdf2_sha256$..."          # administrator

[auth.kibo]
backoffice = "pbkdf2_sha256$..."     # Kibo cancellations only

[auth.warehouse]
backoffice = "pbkdf2_sha256$..."     # Warehouse cancellations only
```

Generate each line with `python tools/make_password_hash.py <username> [app]`;
with an app it prints the `[auth.<app>]` header too. The sidebar is built from
what the account may open, so a user never sees a tool they cannot use, and the
page refuses them anyway if they reach it by its URL. Adding a tool later means
adding an `[auth.<new_app>]` section — the administrator needs no change.

## Layout

```
streamlit_app.py                     the front door: sign-in, access, navigation
pages/1_Kibo_cancellations.py        one file per tool, the order is the sidebar order
pages/2_Warehouse_cancellations.py
shared/console.py                    sign-in and access, GitHub client, CSV plumbing, Logs and Folders tabs
tools/make_password_hash.py          generates the login hashes
.streamlit/secrets.toml.example      what the deployment needs
requirements.txt
```

A page supplies only what is particular to it: its columns, its aliases, its
entry form and its queue folders. Everything else comes from `shared/console.py`.

## Deploy

1. Push this folder to its own repository, named `applications`. It can be
   public: there are no secrets and no data in it.
2. On [share.streamlit.io](https://share.streamlit.io): **Create app** → this
   repo, branch `main`, main file `streamlit_app.py`. The pages appear in the
   sidebar on their own.
3. **Advanced settings → Secrets**: paste a filled-in copy of
   `.streamlit/secrets.toml.example`.
   - `[github] token`: a fine-grained personal access token scoped to the
     **operational_tools** repository only, with `Contents: Read and write`.
   - `[github] repo`: `your-org/operational_tools`.
   - `[kibo]` and `[warehouse]`: the queue folders of each tool. The defaults in
     the code already match the repository layout, so these sections are only
     needed if you move a queue.
   - `[auth]`: the administrator; `[auth.kibo]` and `[auth.warehouse]`: the
     per-app users. See *Who can open what* above.
4. Deploy. The first load takes a few seconds; the app sleeps after a long idle
   and wakes on the next visit.

Locally:

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit it
streamlit run streamlit_app.py
```

## Adding a tool

Add a file under `pages/`, numbered for its place in the sidebar. Give it its
columns, its aliases and its queue folders, and call `settings()`,
`start_page(..., app_key=APP_KEY, label=...)`, `submit_rows()`, `tab_logs()`
and `tab_queue()` from `shared/console.py`. Then add it to `TOOLS` in
`streamlit_app.py`, add an `[auth.<key>]` section for its users and, if the
defaults in the file are not what you want, a folder section. Finally add the
worker and its workflow in operational_tools, writing to a queue folder of its
own. The administrator account picks the new tool up on its own.

## Worth knowing

**One sign-in.** The session is shared across the pages an account may open, so
moving between tools does not ask again. Signing out on any page clears the
session everywhere.

**Separate passwords, one token.** The logins decide which page someone sees,
not what the GitHub token can do: every page commits with the same token. The
separation is about who queues what, not about containing a stolen token.

**Password security.** The logins are a shared secret checked against PBKDF2
hashes — good enough to keep the pages off the open internet, not a substitute
for real accounts. Anyone with a login can cancel anything the stored
credentials can reach. Keep the list short and rotate it when people leave.

**The token is the blast radius.** One token serves every page, so scope it to
the single data repository and nothing else.
