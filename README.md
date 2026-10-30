# Quote & PO Hub

Reads company **quotation and purchase-order emails from Gmail**, converts whatever format they arrive in
(PDF, scanned image, Excel, CSV, Word, HTML, plain text, text in the email body) into **one standard JSON
format**, stores it, and shows it in a web dashboard. Every time documents are pulled from Gmail into the
app, an **in-app notification** appears (toast + bell). Runs unattended; a person only looks at documents the
system is unsure about.

```
Gmail (read-only API) ─┐
Folder drop / Upload ──┼─► parsers (any format → text) ─► extractor (Claude AI, rules fallback)
                       │      ─► standard Document JSON ─► checks (totals add up?) ─► SQLite
                       │      ─► status: approved  |  needs_review  ─► 🔔 in-app notification
```

## Run it (2 minutes, no Gmail needed)

```bash
pip install -r requirements.txt            # also needs the tesseract OCR binary for scanned files
cp .env.example .env
python scripts/make_samples.py             # drops 9 sample files (pdf/xlsx/csv/docx/png/txt) into data/inbox
uvicorn app.main:app --port 8000           # open http://localhost:8000
```

The samples are picked up automatically and you will see toasts and the bell light up. You can also drag
files onto the page, or click **Upload files**. Tests: `pytest`.

## Connect Gmail (once)

1. Google Cloud Console → create a project → enable the **Gmail API**.
2. *APIs & Services → OAuth consent screen*: User type **Internal** (Google Workspace) or External + add yourself as a test user.
3. *Credentials → Create credentials → OAuth client ID → Desktop app* → download as `credentials.json` into this folder.
4. `python -m app.gmail_auth` → a browser opens, approve **read-only** access → `token.json` is saved.
5. Restart the app. It now checks Gmail every `POLL_SECONDS` (default 120 s). **Sync Gmail** forces a check.

`GMAIL_QUERY` in `.env` is only the **time window** (default `newer_than:7d`; use `newer_than:30d` once to back-fill).
*Which* mails are wanted is decided by the filters below. Nothing is ever sent, deleted or modified in Gmail.

## Choosing which emails are processed (Filters)

Click **Filters** in the app. Mail that doesn't pass is **ignored**: it is not downloaded, not read by the AI,
and never shows up in your review queue.

| Setting | What it does |
|---|---|
| **Always process emails from** | Your suppliers / customers: an address (`buyer@acme.com`) or a whole company (`acme.com`). They pass whatever the subject says. |
| **Only these senders** | Strict mode: everyone not on that list is ignored, even if the subject says "quotation". Safest once you know your regular senders. |
| **Never process emails from** | Newsletters, marketing, alerts. `noreply@` blocks every address starting with it. A blocked sender always wins. |
| **Looks like a quotation / PO when the subject or file name contains** | For senders not on your list. Whole words only: `po` matches `PO-4521`, not "report" or "popular". |
| **Ignore when the subject contains** | e.g. newsletter, webinar, unsubscribe, out of office. A blocked word always ignores, even for allowed senders. |
| **Only emails with an attachment** | Skips plain-text chit-chat. |
| **Gmail label** | Only look inside one label (make a Gmail filter that labels quotation/PO mail `QuotePO`). |

The **Try it before saving** box shows what would happen to a sample sender / subject / file name, with the reason.

Nothing is silently lost: the **Ignored** button lists every skipped email with the reason, and offers
**Process anyway** (one-off) or **Always allow sender**. Saving new rules re-checks recent mail straight away.

How little is read: mail is judged first from its sender and subject only; mail with no attachment that
still fails the rules is dropped from those headers and **never opened**; attachments are downloaded only for
mail that passed. Already-handled mail is never fetched twice.

## Turn on AI extraction (strongly recommended)

Put `ANTHROPIC_API_KEY` in `.env`. Without it the app uses a regex fallback that works on clean layouts but is
much less accurate, so **every document is deliberately sent to review** until a key is set. With a key, Claude
reads any vendor layout and returns the standard format; the arithmetic checks still run on top.

## The standard format (`app/schema.py`)

`doc_type` (quotation / purchase_order), `doc_number`, `doc_date`, `valid_until_or_delivery_date`,
`reference_number` (a PO points at its quotation → shown as *Linked* in the dashboard), `currency`,
`payment_terms`, `vendor` and `buyer` (name, tax id, address, email, phone), `line_items`
(description, code, qty, unit, unit price, tax %, line total), `subtotal`, `tax_total`, `grand_total`.

## What needs a human

A document goes to **Needs review** when confidence is below `REVIEW_THRESHOLD` or a check fails: missing number
or total, qty × price ≠ line total, lines ≠ subtotal, subtotal + tax ≠ grand total, type unclear, unreadable file.
Open it, fix fields in the side panel, **Approve** / **Reject**. Corrections re-run the checks.
Files that are clearly not documents (logos, chit-chat) are ignored quietly, and nothing is processed twice.

## Notifications

Written to the database each time documents arrive, then shown live in the page (polled every 4 s): a toast per
new document, one summary toast for a big batch, the 🔔 unread counter, and a browser notification if the tab is in
the background. Errors (failed sync, unreadable file) notify too. To also push to Slack / email / WhatsApp, add a
call next to `db.add_notification` in `app/pipeline.py`.

## Files

| Path | Purpose |
|---|---|
| `app/filters.py` | which emails are wanted (rules, matching, Gmail search narrowing) |
| `app/gmail_client.py` | Gmail API reader: headers first, opens / downloads only wanted mail |
| `app/parsers.py` | any file → text (PDF, OCR, xlsx/xls, csv, docx, html, txt; sniffs wrong extensions) |
| `app/extractor.py` | text → standard document (Claude, rules fallback) |
| `app/validate.py` | arithmetic / completeness checks and confidence score |
| `app/pipeline.py` | ties it together, dedupes, notifies |
| `app/db.py` | SQLite (10–20 emails/day is tiny; swap this one file for Postgres if you grow) |
| `app/main.py`, `app/static/index.html` | API + dashboard |

## Deploying

Run it on a small always-on machine (VPS, Cloud Run with a volume, or an office PC) so the background sync keeps going:
`uvicorn app.main:app --host 0.0.0.0 --port 8000` behind HTTPS. **The dashboard has no login yet** — put it behind
your VPN / a reverse proxy with authentication before exposing it to the internet; it contains company pricing.
Keep `credentials.json`, `token.json` and `.env` out of git.

## Known limits

* Gmail is polled (default every 2 min), not pushed; for 10–20 mails/day that is plenty.
* The AI path needs an Anthropic API key and was not exercised here (no key in the build environment); the Gmail
  connection could not be exercised either (needs your Google credentials). Everything else is covered by tests.
* OCR quality depends on scan quality; poor scans land in the review queue.
