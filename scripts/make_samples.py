"""Generate sample quotations / POs in many formats into data/inbox (or a folder you pass).

    python scripts/make_samples.py [target_folder]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "inbox"
OUT.mkdir(parents=True, exist_ok=True)

ITEMS = [("Steel bracket 40mm", 100, 25.00), ("Hex bolt M8 (box of 50)", 20, 180.00), ("Powder coating service", 1, 1500.00)]
SUB = sum(q * p for _, q, p in ITEMS)          # 7600
TAX = round(SUB * 0.18, 2)                     # 1368
TOTAL = SUB + TAX                              # 8968


def text_doc(kind: str, number_label: str, number: str, ref_line: str = "") -> str:
    rows = "\n".join(f"{d} | {q} | {p:.2f} | {q * p:.2f}" for d, q, p in ITEMS)
    return f"""{kind}
{number_label}: {number}
Date: 2026-10-05
{ref_line}Vendor: Sharma Fasteners Pvt Ltd
Buyer: Acme Engineering Works
Vendor GSTIN: 27ABCDE1234F1Z5
Buyer GSTIN: 29PQRSX9876L1Z2
Payment Terms: Net 30 days
Delivery Date: 2026-11-01

Description | Qty | Unit Price | Amount
{rows}

Subtotal: INR {SUB:,.2f}
GST 18%: INR {TAX:,.2f}
Grand Total: INR {TOTAL:,.2f}
"""


def make_pdf(path: Path, kind: str, label: str, number: str, ref_line: str = "") -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

    st = getSampleStyleSheet()
    flow = [Paragraph(kind, st["Title"])]
    for line in text_doc(kind, label, number, ref_line).splitlines()[1:]:
        if "|" in line or not line.strip():
            continue
        flow.append(Paragraph(line, st["Normal"]))
    flow.append(Spacer(1, 12))
    data = [["Description", "Qty", "Unit Price", "Amount"]] + [[d, q, f"{p:.2f}", f"{q * p:.2f}"] for d, q, p in ITEMS]
    t = Table(data)
    t.setStyle([("GRID", (0, 0), (-1, -1), 0.5, "black")])
    flow.append(t)
    SimpleDocTemplate(str(path), pagesize=A4).build(flow)


def main() -> None:
    # 1. PDF quotation
    make_pdf(OUT / "quotation_QT-2026-0042.pdf", "QUOTATION", "Quotation No", "QT-2026-0042")
    # 2. PDF purchase order referencing the quotation (shows quote<->PO linking)
    make_pdf(OUT / "PO_4521.pdf", "PURCHASE ORDER", "PO Number", "PO-4521", "Against Quotation: QT-2026-0042\n")

    # 3. Excel quotation
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quote"
    for row in [["QUOTATION"], ["Quotation No", "QT-2026-0050"], ["Date", "2026-10-06"],
                ["Vendor", "Verma Packaging Co"], ["Buyer", "Acme Engineering Works"], ["Currency", "INR"],
                [], ["Description", "Qty", "Unit Price", "Amount"]]:
        ws.append(row)
    for d, q, p in ITEMS:
        ws.append([d, q, p, q * p])
    ws.append([]); ws.append(["Subtotal", SUB]); ws.append(["GST 18%", TAX]); ws.append(["Grand Total", TOTAL])
    wb.save(OUT / "quote_verma.xlsx")

    # 4. CSV purchase order
    with open(OUT / "po_export.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["PURCHASE ORDER"]); w.writerow(["PO Number", "PO-4530"]); w.writerow(["Date", "2026-10-06"])
        w.writerow(["Vendor", "Sharma Fasteners Pvt Ltd"]); w.writerow(["Buyer", "Acme Engineering Works"])
        w.writerow([]); w.writerow(["Description", "Qty", "Unit Price", "Amount"])
        for d, q, p in ITEMS:
            w.writerow([d, q, p, q * p])
        w.writerow([]); w.writerow(["Subtotal", SUB]); w.writerow(["GST 18%", TAX]); w.writerow(["Grand Total", TOTAL])

    # 5. Word quotation
    import docx
    d = docx.Document()
    d.add_heading("QUOTATION", 1)
    for line in ["Quotation No: QT-2026-0061", "Date: 2026-10-07", "Vendor: Rao Industrial Supplies",
                 "Buyer: Acme Engineering Works", "Payment Terms: 50% advance"]:
        d.add_paragraph(line)
    tbl = d.add_table(rows=1, cols=4)
    for i, h in enumerate(["Description", "Qty", "Unit Price", "Amount"]):
        tbl.rows[0].cells[i].text = h
    for desc, q, p in ITEMS:
        cells = tbl.add_row().cells
        for i, v in enumerate([desc, str(q), f"{p:.2f}", f"{q * p:.2f}"]):
            cells[i].text = v
    d.add_paragraph(f"Subtotal: INR {SUB:,.2f}")
    d.add_paragraph(f"GST 18%: INR {TAX:,.2f}")
    d.add_paragraph(f"Grand Total: INR {TOTAL:,.2f}")
    d.save(OUT / "rao_quote.docx")

    # 6. Plain-text PO (as if typed in an email body)
    (OUT / "po_from_email_body.txt").write_text(text_doc("PURCHASE ORDER", "PO Number", "PO-4544"))

    # 7. Scanned-style image (needs OCR)
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1100, 700), "white")
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    y = 30
    for line in text_doc("QUOTATION", "Quotation No", "QT-2026-0077").splitlines():
        dr.text((40, y), line, fill="black", font=font)
        y += 32
    img.save(OUT / "scanned_quote.png")

    # 8. Deliberately WRONG totals -> must land in the review queue
    (OUT / "bad_totals_PO.txt").write_text(
        text_doc("PURCHASE ORDER", "PO Number", "PO-4599").replace(f"{TOTAL:,.2f}", "99,999.00"))

    # 9. Not a document at all -> must be ignored quietly
    (OUT / "lunch_menu.txt").write_text("Hi team, lunch is at 1pm today in the cafeteria. See you there!")

    print(f"Wrote samples to {OUT}")


if __name__ == "__main__":
    main()
