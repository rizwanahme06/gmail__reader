import io
import json

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import config, db, parsers, pipeline
from app.validate import check
from app.schema import Document, LineItem


# ---------- parsers: every format becomes text containing the document number ----------

@pytest.mark.parametrize("name,needle", [
    ("quotation_QT-2026-0042.pdf", "QT-2026-0042"),
    ("quote_verma.xlsx", "QT-2026-0050"),
    ("po_export.csv", "PO-4530"),
    ("rao_quote.docx", "QT-2026-0061"),
    ("po_from_email_body.txt", "PO-4544"),
    ("scanned_quote.png", "QT-2026-0077"),   # OCR
])
def test_parsers_read_every_format(samples, name, needle):
    text, parser = parsers.extract_text(name, (samples / name).read_bytes())
    assert needle in text, (parser, text[:200])


def test_wrong_extension_is_sniffed(samples):
    data = (samples / "quotation_QT-2026-0042.pdf").read_bytes()
    text, _ = parsers.extract_text("attachment.dat", data)
    assert "QT-2026-0042" in text


def test_corrupt_file_never_raises():
    text, parser = parsers.extract_text("broken.pdf", b"%PDF-1.4 not really a pdf")
    assert text == "" and parser.startswith("failed")


# ---------- validation ----------

def test_validation_flags_bad_arithmetic():
    d = Document(doc_type="purchase_order", doc_number="P1", subtotal=100, tax_total=18, grand_total=500,
                 line_items=[LineItem(description="x", quantity=2, unit_price=10, line_total=20)])
    assert any("grand total" in i for i in check(d))


def test_validation_passes_clean_doc():
    d = Document(doc_type="quotation", doc_number="Q1", subtotal=100, tax_total=18, grand_total=118,
                 line_items=[LineItem(description="x", quantity=2, unit_price=50, line_total=100)])
    assert check(d) == []


# ---------- pipeline ----------

def _ingest_all(samples):
    files = [(p.name, p.read_bytes()) for p in sorted(samples.iterdir()) if p.is_file()]
    return pipeline.ingest_message(
        {"source_key": "gmail:m1", "message_id": "m1", "sender": "Sharma", "subject": "Docs", "received_at": db.now()}, files)


def test_pipeline_converts_all_formats_and_skips_non_documents(samples, fresh_db):
    r = _ingest_all(samples)
    assert r["added"] == 8 and r["failed"] == 0            # lunch_menu.txt skipped
    with db.conn() as c:
        rows = {r["filename"]: dict(r) for r in c.execute("SELECT * FROM documents")}
    assert "lunch_menu.txt" not in rows
    for name, r in rows.items():
        assert r["doc_number"], name
        assert r["grand_total"] == (99999.0 if name == "bad_totals_PO.txt" else 8968.0), name
        assert json.loads(r["data"])["line_items"], name    # same standard JSON everywhere
    assert rows["PO_4521.pdf"]["reference_number"] == "QT-2026-0042"
    assert rows["quote_verma.xlsx"]["vendor_name"] == "Verma Packaging Co"      # no stray table separators
    assert rows["po_export.csv"]["buyer_name"] == "Acme Engineering Works"
    assert any("grand total" in i for i in json.loads(rows["bad_totals_PO.txt"]["issues"]))
    assert rows["bad_totals_PO.txt"]["status"] == "needs_review"


def test_reprocessing_does_not_duplicate(samples, fresh_db):
    _ingest_all(samples)
    again = _ingest_all(samples)
    assert again["added"] == 0
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 8


def test_one_bad_attachment_does_not_stop_the_batch(samples, fresh_db):
    good = (samples / "quotation_QT-2026-0042.pdf").read_bytes()
    r = pipeline.ingest_message(
        {"source_key": "gmail:m2", "message_id": "m2", "sender": "X", "subject": "S", "received_at": db.now()},
        [("broken.pdf", b"%PDF-1.4 garbage"), ("good.pdf", good)])
    assert r["added"] == 1 and r["failed"] == 1


def test_document_in_email_body_and_chitchat(fresh_db):
    body = "PURCHASE ORDER\nPO Number: PO-7001\nDate: 2026-10-06\nVendor: A Co\nBuyer: B Co\n" \
           "Item | 2 | 50.00 | 100.00\nSubtotal: 100.00\nGrand Total: 100.00"
    meta = {"source_key": "gmail:m3", "message_id": "m3", "sender": "A", "subject": "PO", "received_at": db.now()}
    assert pipeline.ingest_message(meta, [], body)["added"] == 1
    meta2 = {**meta, "source_key": "gmail:m4"}
    assert pipeline.ingest_message(meta2, [], "Hi, are we still on for lunch tomorrow?")["added"] == 0


def test_sync_creates_in_app_notifications(samples, fresh_db):
    config.INBOX_DIR.mkdir(exist_ok=True)
    for p in samples.iterdir():
        (config.INBOX_DIR / p.name).write_bytes(p.read_bytes())
    res = pipeline.sync_folder()
    assert res["added"] == 8
    with db.conn() as c:
        kinds = [r["kind"] for r in c.execute("SELECT kind FROM notifications")]
    assert kinds.count("new_document") == 8 and kinds.count("sync") == 1


# ---------- API ----------

@pytest.fixture()
def client(samples, fresh_db):
    from app.main import app
    with TestClient(app) as c:
        _ingest_all(samples)
        yield c


def test_api_list_filter_and_detail(client):
    docs = client.get("/api/documents").json()
    assert len(docs) == 8
    assert len(client.get("/api/documents?doc_type=quotation").json()) == 4
    assert len(client.get("/api/documents?q=PO-4599").json()) == 1
    d = client.get(f"/api/documents/{docs[0]['id']}").json()
    assert "line_items" in d["data"]
    assert client.get("/api/documents/9999").status_code == 404


def test_api_correct_and_approve(client):
    bad = client.get("/api/documents?q=PO-4599").json()[0]
    data = client.get(f"/api/documents/{bad['id']}").json()["data"]
    data["grand_total"] = 8968.0
    r = client.patch(f"/api/documents/{bad['id']}", json={"data": data, "status": "approved"}).json()
    assert r["status"] == "approved" and r["issues"] == [] or all("grand total" not in i for i in r["issues"])
    assert client.patch(f"/api/documents/{bad['id']}", json={"status": "bogus"}).status_code == 400


def test_api_po_quote_link(client):
    links = client.get("/api/links").json()
    assert [(l["po_number"], l["quote_number"]) for l in links] == [("PO-4521", "QT-2026-0042")]


def test_api_notifications_and_stats(client):
    s = client.get("/api/stats").json()
    assert s["by_status"]["needs_review"] == 8 and s["gmail_connected"] is False
    db.add_notification("sync", "hello")
    n = client.get("/api/notifications?unread_only=true").json()
    assert n and n[0]["title"] == "hello"
    client.post("/api/notifications/read")
    assert client.get("/api/notifications?unread_only=true").json() == []


def test_api_export_xlsx(client):
    r = client.get("/api/export.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb["Documents"].max_row == 9 and wb["Line items"].max_row == 1 + 8 * 3


def test_api_upload_any_file(client, samples):
    f = ("fresh.docx", (samples / "rao_quote.docx").read_bytes(), "application/octet-stream")
    r = client.post("/api/upload", files=[("files", f)]).json()
    assert r["added"] == 1
    assert client.get("/api/documents?q=QT-2026-0061").json()


def test_original_file_download(client):
    d = client.get("/api/documents?q=QT-2026-0042").json()[0]
    r = client.get(f"/api/documents/{d['id']}/file")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Quote &amp; PO Hub" in r.text


def test_sync_without_gmail_gives_clear_error(client):
    r = client.post("/api/sync?source=gmail")
    assert r.status_code == 400 and "not connected" in r.json()["detail"]
