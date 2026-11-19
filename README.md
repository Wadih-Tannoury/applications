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
| **Warehouse cancellations** | A whole shipment to stop at the GXO warehouse, and nothing else. The event has no item granularity, so this page deals in shipments only — a single item, or anything in Kibo, is the Kibo page's job | `warehouse/input/` |
| **Cancellation ledger** | Nothing. It is the read-only window onto what the warehouse cancellations have done | reads BigQuery |
| **Warehouse cancellations (staging)** | The same as the Warehouse page. A working copy, open to its owner only, where a change to the flow is tried first | `warehouse/input/` |

The Warehouse page asks for one thing, the `shipmentOrderNumber`, on both of
its entry tabs. Every row is queued with `cancelInKibo=no`, so the shipment is
left untouched in Kibo whatever the workflow's own Kibo switch is set to, and
the warehouse event always goes out with `reasonMessage` `Cancelled`, which is
why nothing asks for one.

The two queueing pages have the same four tabs: **Upload CSV**, **Single
cancellation**, **Logs** and **Folders**. The Logs and Folders tabs only ever
look at that tool's own folders, so the two queues never show each other's
files.

### Cancellation ledger

Shows `tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table —
a row per shipment the operational_tools repository has cancelled at the GXO
warehouse, and what became of it since. One row per shipment, with the columns
the table holds and a **Stage** telling you where it has got to:

| Stage | Meaning |
|---|---|
| Not sent | On the ledger, but the CANCEL event never reached the warehouse |
| Awaiting the warehouse | Sent; no CANCELLED event back yet |
| To cancel in Kibo | The warehouse confirmed it; the Kibo shipment is still open |
| Done | Cancelled at the warehouse and in Kibo |

A row awaiting the warehouse for more than 24 hours is marked ⚠ — the same
window the Reconcile cancellations workflow uses, so an overdue row is one that
workflow will resend on its next run.

**The table is read afresh on every run of the page script**, which Streamlit
does on sign-in, on refresh and whenever a filter changes. Nothing is cached, so
what is on screen is what BigQuery held at the moment the page drew, and the
footer says when that was. One query per page view is what this costs; if that
ever matters, `fetch_rows` in `shared/ledger.py` is the single place to put a
`ttl` on.

Filtering by period and search runs in BigQuery rather than in the page, so a
long ledger is never shipped here in full; the Stage filter then narrows what
came back. The page reads and nothing else: it has no write path to the ledger
at all, and the service account it signs in with should have no write role
either.

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
pages/3_Cancellation_ledger.py
pages/4_Warehouse_cancellations_staging.py   a copy of page 2, on purpose: see Staging below
shared/console.py                    sign-in and access, GitHub client, CSV plumbing, Logs and Folders tabs
shared/ledger.py                     the BigQuery read behind the Cancellation ledger page
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
   - `[auth]`: the administrator; `[auth.kibo]`, `[auth.warehouse]` and
     `[auth.ledger]`: the per-app users. See *Who can open what* above.
   - `[staging] users`: who may open the staging pages. Defaults to `Wadih`
     when the section is absent. See *Staging* below.
   - `[bigquery]`: the service-account JSON for the Cancellation ledger page,
     pasted field by field. It needs `roles/bigquery.dataViewer` on
     `tlg-business-intelligence-prd.til` and `roles/bigquery.jobUser` on the
     project — **and no write role**, so the console cannot alter the ledger
     even by accident. Without this section every other page still works and
     the ledger page says what is missing.
4. Deploy. The first load takes a few seconds; the app sleeps after a long idle
   and wakes on the next visit.

Locally:

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit it
streamlit run streamlit_app.py
```

## Staging

`pages/4_Warehouse_cancellations_staging.py` is a **standalone copy** of the
warehouse page, not a variant of it. Nothing imports it and it imports nothing
from page 2, so it can be rewritten, broken and rewritten again without any of
that reaching the people using the live page. That duplication is the point: a
staging page that shared code with the live one could not be changed freely,
which is the only thing it is for.

**Who sees it.** The `[staging] users` list in the secrets, and nobody else.
Being an administrator is deliberately not enough — `"*"` does not reach a
staging page, or every administrator would be walked into a half-finished one.
An administrator who is not on the list does not see it in the sidebar and is
refused if they reach it by its URL. There is no `[auth.<app>]` section for it
and granting its key does nothing.

**Where it writes.** With no `[warehouse_staging]` section in the secrets it
follows the live page exactly: same repository, same branch, same
`warehouse/input/`. A submission there is a **real cancellation**, picked up by
the production workflow and really stopped at GXO — which is what makes it a
test of the flow rather than a rehearsal of it. The page carries a banner naming
the queue it is about to write to. To isolate it instead, add a
`[warehouse_staging]` section with its own `branch` or folders; note that a
folder the warehouse workflow does not watch means nothing ever processes what
lands there.

**Promoting a change.** Once it has proved itself, the staging file becomes the
live one:

```bash
cp pages/4_Warehouse_cancellations_staging.py pages/2_Warehouse_cancellations.py
```

then put back the four lines that differ — the docstring, `APP_KEY`, `STAGING`
and the `start_page` call — each marked `STAGING:` in the file. Keep the staging
copy where it is; it is the starting point for the next change, not a branch to
delete.

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
