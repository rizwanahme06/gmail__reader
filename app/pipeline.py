"""email/file -> text -> standard Document -> database -> notification."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import config, db
from .extractor import extract
from .parsers import extract_text

MIN_TEXT = 30  # ignore logos / signature images that OCR to nothing


def _label(d) -> str:
    kind = {"purchase_order": "PO", "quotation": "Quotation"}.get(d.doc_type, "Document")
    return f"{kind} {d.doc_number or '(no number)'}"


def _money(d) -> str:
    if d.grand_total is None:
        return ""
    return f"{d.currency or ''} {d.grand_total:,.2f}".strip()


_DOC_WORDS = re.compile(r"quot|purchase\s*order|\bPO\b|invoice|proforma|estimate|unit\s*price|grand\s*total|subtotal", re.I)


def _nothing_found(d, text: str) -> bool:
    """True for files that are clearly not quotations/POs (logos, signatures, chit-chat):
    nothing extracted AND no document vocabulary in the text. If the text *does* look like a
    document but extraction failed, we keep it so a human reviews it instead of losing it."""
    empty = d.doc_type == "unknown" and not d.line_items and d.grand_total is None and not d.doc_number
    return empty and not _DOC_WORDS.search(text)


def ingest_message(meta: dict, attachments: list[tuple[str, bytes]], body_text: str = "") -> dict:
    """Process one email. Returns {'added': n, 'failed': n, 'ids': [...]}.
    Safe to call repeatedly: already-processed attachments are skipped."""
    out = {"added": 0, "failed": 0, "ids": []}
    ctx = f"From: {meta.get('sender')}\nSubject: {meta.get('subject')}\nDate: {meta.get('received_at')}\nBody:\n{body_text[:1500]}"

    candidates: list[tuple[str, bytes | None, str | None]] = [(n, d, None) for n, d in attachments]
    if body_text.strip():
        candidates.append(("email-body.txt", None, body_text))

    attachment_hits = 0
    for name, data, text_override in candidates:
        key = f"{meta['source_key']}:{name}"
        if db.seen(key):
            continue
        is_body = text_override is not None
        # Only fall back to the email body when no attachment produced a document
        if is_body and attachment_hits:
            continue
        try:
            if is_body:
                text, parser = text_override, "email_body"
            else:
                text, parser = extract_text(name, data)
            if len(text.strip()) < MIN_TEXT:
                if not is_body and parser.startswith("failed"):
                    db.insert_document({**meta, "source_key": key}, _failed(parser), name, parser, "failed")
                    out["failed"] += 1
                else:
                    db.mark_skipped(key)
                continue
            ex = extract(text, ctx)
            d = ex.document
            if (is_body and d.doc_type == "unknown" and not d.line_items and d.grand_total is None) or \
                    _nothing_found(d, text):
                db.mark_skipped(key)  # ordinary chit-chat email / unrelated attachment, not a document
                continue
            status = "needs_review" if (ex.confidence < config.REVIEW_THRESHOLD or ex.issues) else "approved"
            if ex.confidence >= config.REVIEW_THRESHOLD and ex.issues and all(
                i.startswith("No ANTHROPIC_API_KEY") for i in ex.issues
            ):
                status = "needs_review"
            doc_id = db.insert_document({**meta, "source_key": key}, ex, name, parser, status)
            out["added"] += 1
            out["ids"].append(doc_id)
            if not is_body:
                attachment_hits += 1
                (config.ATTACH_DIR / f"{doc_id}_{Path(name).name}").write_bytes(data)
            tag = "⚠ needs review" if status == "needs_review" else "✓ ready"
            db.add_notification(
                "new_document",
                f"New {_label(d)} from {meta.get('sender', 'unknown')}",
                " · ".join(x for x in (_money(d), tag, f"confidence {int(ex.confidence * 100)}%") if x),
                doc_id,
            )
        except Exception as e:  # one bad file must never stop the batch
            out["failed"] += 1
            db.add_notification("error", f"Could not process {name}", f"{type(e).__name__}: {e}")
    return out


def _failed(parser: str):
    from .schema import Document, Extraction

    return Extraction(document=Document(), confidence=0.0, issues=[f"Could not read file ({parser})"], method="none")


def finish_sync(source: str, started: str, emails_seen: int, added: int, failed: int, error: str | None = None,
                ignored: int = 0) -> dict:
    db.log_sync(source, started, emails_seen, added, failed, error)
    if error:
        db.add_notification("error", f"{source} sync failed", error)
    elif added or failed:
        body = f"{emails_seen} emails checked"
        if ignored:
            body += f" · {ignored} skipped by your filters"
        if failed:
            body += f" · {failed} could not be read"
        db.add_notification("sync", f"{source} sync: {added} new document(s) added to the app", body)
    return {"source": source, "emails_seen": emails_seen, "added": added, "failed": failed, "error": error,
            "ignored": ignored}


# ---------------- folder source (also the demo / test mode) ----------------

def sync_folder() -> dict:
    started = db.now()
    inbox = config.INBOX_DIR
    done = inbox / "processed"
    done.mkdir(exist_ok=True)
    files = [p for p in sorted(inbox.iterdir()) if p.is_file() and not p.name.startswith(".")]
    added = failed = 0
    for p in files:
        meta = {
            "source_key": f"file:{p.name}:{p.stat().st_size}",
            "message_id": f"file:{p.name}",
            "sender": "folder drop",
            "subject": p.name,
            "received_at": db.now(),
        }
        r = ingest_message(meta, [(p.name, p.read_bytes())])
        added += r["added"]
        failed += r["failed"]
        shutil.move(str(p), str(done / p.name))
    return finish_sync("Folder", started, len(files), added, failed)


# ---------------- gmail source ----------------

def sync_gmail() -> dict:
    started = db.now()
    ignored = 0
    try:
        from . import filters, gmail_client

        def on_ignored(meta: dict, reason: str) -> None:
            nonlocal ignored
            ignored += 1
            db.add_ignored(meta, reason)

        seen_n = added = failed = 0
        for meta, atts, body in gmail_client.fetch_new(db.message_known, db.seen, filters.load(), on_ignored):
            seen_n += 1
            r = ingest_message(meta, atts, body)
            added += r["added"]
            failed += r["failed"]
        return finish_sync("Gmail", started, seen_n, added, failed, ignored=ignored)
    except Exception as e:
        return finish_sync("Gmail", started, 0, 0, 0, f"{type(e).__name__}: {e}", ignored=ignored)


def process_gmail_message(message_id: str) -> dict:
    """'Process anyway' for one email the filters ignored. Bypasses the filters, same pipeline after that."""
    started = db.now()
    try:
        from . import gmail_client

        meta, atts, body = gmail_client.fetch_one(message_id, db.seen)
        r = ingest_message(meta, atts, body)
        db.delete_ignored(message_id)
        return finish_sync("Gmail (manual)", started, 1, r["added"], r["failed"])
    except Exception as e:
        return finish_sync("Gmail (manual)", started, 0, 0, 0, f"{type(e).__name__}: {e}")
