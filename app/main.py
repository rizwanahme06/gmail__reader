from __future__ import annotations

import io
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import config, db, filters, pipeline
from .schema import Document
from .validate import check

STATIC = Path(__file__).parent / "static"
_state = {"last_gmail": None, "last_folder": None, "gmail_connected": False}
_stop = threading.Event()
_sync_lock = threading.Lock()


def _gmail_ready() -> bool:
    return config.GMAIL_TOKEN_FILE.exists()


def _loop() -> None:
    last_gmail = 0.0
    while not _stop.is_set():
        try:
            with _sync_lock:
                if any(p.is_file() and not p.name.startswith(".") for p in config.INBOX_DIR.iterdir()):
                    pipeline.sync_folder()
                    _state["last_folder"] = db.now()
                if _gmail_ready() and time.time() - last_gmail >= config.POLL_SECONDS:
                    last_gmail = time.time()
                    pipeline.sync_gmail()
                    _state["last_gmail"] = db.now()
        except Exception as e:  # keep the watcher alive no matter what
            db.add_notification("error", "Background sync error", f"{type(e).__name__}: {e}")
        _stop.wait(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    yield
    _stop.set()


app = FastAPI(title="Quote & PO Hub", lifespan=lifespan)


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ---------------- pages ----------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# ---------------- documents ----------------

@app.get("/api/documents")
def list_documents(status: str = "", doc_type: str = "", q: str = ""):
    sql, args = "SELECT * FROM documents WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if doc_type:
        sql += " AND doc_type=?"; args.append(doc_type)
    if q:
        like = f"%{q}%"
        sql += " AND (doc_number LIKE ? OR vendor_name LIKE ? OR buyer_name LIKE ? OR sender LIKE ? OR subject LIKE ?)"
        args += [like] * 5
    sql += " ORDER BY id DESC LIMIT 500"
    rows = _rows(sql, tuple(args))
    for r in rows:
        r["issues"] = json.loads(r["issues"] or "[]")
        r.pop("data", None)
    return rows


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int):
    rows = _rows("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not rows:
        raise HTTPException(404, "Not found")
    r = rows[0]
    r["issues"] = json.loads(r["issues"] or "[]")
    r["data"] = json.loads(r["data"])
    return r


class DocUpdate(BaseModel):
    status: str | None = None
    data: dict | None = None  # full standard document, as corrected by a human


@app.patch("/api/documents/{doc_id}")
def update_document(doc_id: int, body: DocUpdate):
    cur = get_document(doc_id)
    sets, args = [], []
    if body.status:
        if body.status not in ("approved", "rejected", "needs_review"):
            raise HTTPException(400, "Bad status")
        sets.append("status=?"); args.append(body.status)
    if body.data is not None:
        d = Document.model_validate(body.data)
        sets += ["data=?", "doc_type=?", "doc_number=?", "doc_date=?", "due_date=?", "reference_number=?",
                 "currency=?", "payment_terms=?", "vendor_name=?", "buyer_name=?", "subtotal=?", "tax_total=?",
                 "grand_total=?", "issues=?"]
        # re-run the arithmetic / completeness checks on the human-corrected data
        args += [d.model_dump_json(), d.doc_type, d.doc_number, d.doc_date, d.valid_until_or_delivery_date,
                 d.reference_number, d.currency, d.payment_terms, d.vendor.name, d.buyer.name, d.subtotal,
                 d.tax_total, d.grand_total, json.dumps(check(d))]
    if sets:
        with db.conn() as c:
            c.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id=?", (*args, doc_id))
    return get_document(doc_id)


@app.get("/api/documents/{doc_id}/file")
def original_file(doc_id: int):
    r = get_document(doc_id)
    matches = list(config.ATTACH_DIR.glob(f"{doc_id}_*"))
    if not matches:
        raise HTTPException(404, "No original attachment stored (document came from the email body)")
    return FileResponse(matches[0], filename=r["filename"])


@app.get("/api/links")
def linked_documents():
    """Match POs to the quotation they reference (by reference number)."""
    return _rows(
        """SELECT po.id AS po_id, po.doc_number AS po_number, q.id AS quote_id, q.doc_number AS quote_number,
                  po.grand_total AS po_total, q.grand_total AS quote_total
           FROM documents po JOIN documents q ON po.reference_number = q.doc_number
           WHERE po.doc_type='purchase_order' AND q.doc_type='quotation'"""
    )


# ---------------- filters: which emails are wanted ----------------

@app.get("/api/filters")
def get_filters():
    return filters.load().model_dump()


def _resync_in_background() -> None:
    def run():
        with _sync_lock:
            if _gmail_ready():
                pipeline.sync_gmail()
    threading.Thread(target=run, daemon=True).start()


@app.put("/api/filters")
def put_filters(rules: filters.Rules):
    filters.save(rules)
    db.clear_ignored()          # the new rules may now accept mail that was ignored before
    _resync_in_background()     # re-check recent mail right away instead of waiting for the next poll
    return rules.model_dump()


class FilterTest(BaseModel):
    rules: filters.Rules | None = None   # unsaved rules from the dialog; omitted = the saved ones
    sender: str = ""
    subject: str = ""
    filenames: list[str] = []


@app.post("/api/filters/test")
def test_filters(body: FilterTest):
    ok, reason = filters.decide(body.rules or filters.load(), body.sender.strip(), body.subject,
                                has_attachment=bool(body.filenames), filenames=body.filenames)
    return {"process": ok, "reason": reason}


@app.get("/api/ignored")
def ignored_emails():
    return db.list_ignored()


@app.post("/api/ignored/{message_id}/process")
def process_ignored(message_id: str, allow_sender: bool = False):
    row = db.get_ignored(message_id)
    if not row:
        raise HTTPException(404, "Not in the ignored list (it may already have been processed)")
    if not _gmail_ready():
        raise HTTPException(400, "Gmail is not connected yet. Run: python -m app.gmail_auth")
    if allow_sender and row.get("sender_email"):
        rules = filters.load()
        if row["sender_email"] not in rules.allowed_senders:
            rules.allowed_senders.append(row["sender_email"])
            filters.save(rules)
        # their other ignored mails get re-checked by the next sync, now as a trusted sender
        db.delete_ignored_by_sender(row["sender_email"])
    with _sync_lock:
        return pipeline.process_gmail_message(message_id)


# ---------------- notifications ----------------

@app.get("/api/notifications")
def notifications(unread_only: bool = False, after_id: int = 0):
    sql = "SELECT * FROM notifications WHERE id>?"
    if unread_only:
        sql += " AND is_read=0"
    return _rows(sql + " ORDER BY id DESC LIMIT 50", (after_id,))


@app.post("/api/notifications/read")
def mark_read():
    with db.conn() as c:
        c.execute("UPDATE notifications SET is_read=1")
    return {"ok": True}


# ---------------- status / actions ----------------

@app.get("/api/stats")
def stats():
    by_status = {r["status"]: r["n"] for r in _rows("SELECT status, COUNT(*) n FROM documents GROUP BY status")}
    by_type = {r["doc_type"]: r["n"] for r in _rows("SELECT doc_type, COUNT(*) n FROM documents GROUP BY doc_type")}
    unread = _rows("SELECT COUNT(*) n FROM notifications WHERE is_read=0")[0]["n"]
    last = _rows("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
    return {
        "by_status": by_status, "by_type": by_type, "unread": unread,
        "gmail_connected": _gmail_ready(), "last_sync": last[0] if last else None,
        "ai_enabled": bool(config.ANTHROPIC_API_KEY), "poll_seconds": config.POLL_SECONDS,
        "ignored": db.count_ignored(),
    }


@app.post("/api/sync")
def sync_now(source: str = "gmail"):
    with _sync_lock:
        if source == "gmail":
            if not _gmail_ready():
                raise HTTPException(400, "Gmail is not connected yet. Run: python -m app.gmail_auth")
            return pipeline.sync_gmail()
        return pipeline.sync_folder()


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Manual drop of any file(s) - same pipeline as Gmail attachments."""
    added = failed = 0
    for f in files:
        data = await f.read()
        meta = {"source_key": f"upload:{db.now()}:{f.filename}", "message_id": None,
                "sender": "manual upload", "subject": f.filename, "received_at": db.now()}
        r = pipeline.ingest_message(meta, [(f.filename, data)])
        added += r["added"]; failed += r["failed"]
    return pipeline.finish_sync("Upload", db.now(), len(files), added, failed)


@app.get("/api/export.xlsx")
def export_xlsx(status: str = ""):
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Documents"
    cols = ["id", "doc_type", "doc_number", "doc_date", "due_date", "reference_number", "vendor_name", "buyer_name",
            "currency", "subtotal", "tax_total", "grand_total", "payment_terms", "status", "confidence",
            "sender", "subject", "received_at", "filename"]
    ws.append(cols)
    items = wb.create_sheet("Line items")
    items.append(["doc_id", "doc_number", "doc_type", "description", "item_code", "quantity", "unit",
                  "unit_price", "tax_percent", "line_total"])
    rows = _rows("SELECT * FROM documents" + (" WHERE status=?" if status else "") + " ORDER BY id",
                 (status,) if status else ())
    for r in rows:
        ws.append([r[c] for c in cols])
        for li in json.loads(r["data"]).get("line_items", []):
            items.append([r["id"], r["doc_number"], r["doc_type"], li.get("description"), li.get("item_code"),
                          li.get("quantity"), li.get("unit"), li.get("unit_price"), li.get("tax_percent"),
                          li.get("line_total")])
    for sheet in (ws, items):
        for c in sheet[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F3A5F")
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(40, max(10, max(len(str(c.value or "")) for c in col) + 2))
        sheet.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=quotations_and_pos.xlsx"})
