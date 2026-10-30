"""Turn ANY attachment into plain text (tables rendered as pipe-separated rows).

Dispatch is by file extension first, then by sniffing the first bytes, so
attachments with wrong or missing extensions still work.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

MAX_CHARS = 60_000  # keep extraction prompts bounded


def _clip(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_CHARS]


# ---------- individual parsers ----------

def parse_pdf(data: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            # also pull tables so line items keep their column structure
            for table in page.extract_tables() or []:
                rows = [" | ".join((c or "").replace("\n", " ") for c in row) for row in table]
                txt += "\n[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]"
            parts.append(txt)
    text = "\n\n".join(parts)
    if len(re.sub(r"\s", "", text)) < 40:  # scanned PDF -> OCR
        text = _ocr_pdf(data)
    return _clip(text)


def _ocr_pdf(data: bytes) -> str:
    import pypdfium2 as pdfium
    import pytesseract

    pdf = pdfium.PdfDocument(data)
    out = []
    for i in range(min(len(pdf), 10)):
        img = pdf[i].render(scale=2).to_pil()
        out.append(pytesseract.image_to_string(img))
    return "\n\n".join(out)


def parse_image(data: bytes) -> str:
    import pytesseract
    from PIL import Image

    return _clip(pytesseract.image_to_string(Image.open(io.BytesIO(data))))


def parse_xlsx(data: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"[SHEET {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            if any(c is not None and str(c).strip() for c in row):
                out.append(" | ".join("" if c is None else str(c) for c in row))
    return _clip("\n".join(out))


def parse_xls(data: bytes) -> str:
    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
    out = []
    for name, df in sheets.items():
        out.append(f"[SHEET {name}]")
        out.append(df.fillna("").astype(str).apply(" | ".join, axis=1).str.cat(sep="\n"))
    return _clip("\n".join(out))


def parse_csv(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2048])
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(io.StringIO(text), dialect)
    return _clip("\n".join(" | ".join(r) for r in rows))


def parse_docx(data: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    out = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        out.append("[TABLE]")
        for r in t.rows:
            out.append(" | ".join(c.text.strip().replace("\n", " ") for c in r.cells))
        out.append("[/TABLE]")
    return _clip("\n".join(out))


def parse_html(data: bytes) -> str:
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.out, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
                self.out.append("\n")
            if tag in ("td", "th"):
                self.out.append(" | ")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip = max(0, self.skip - 1)

        def handle_data(self, d):
            if not self.skip:
                self.out.append(d)

    p = _P()
    p.feed(data.decode("utf-8", errors="replace"))
    return _clip("".join(p.out))


def parse_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return _clip(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return ""


def parse_json(data: bytes) -> str:
    try:
        return _clip(json.dumps(json.loads(data), indent=1))
    except Exception:
        return parse_text(data)


# ---------- dispatcher ----------

_EXT = {
    ".pdf": parse_pdf,
    ".png": parse_image, ".jpg": parse_image, ".jpeg": parse_image,
    ".tif": parse_image, ".tiff": parse_image, ".bmp": parse_image, ".webp": parse_image,
    ".xlsx": parse_xlsx, ".xlsm": parse_xlsx,
    ".xls": parse_xls,
    ".csv": parse_csv, ".tsv": parse_csv,
    ".docx": parse_docx,
    ".html": parse_html, ".htm": parse_html,
    ".txt": parse_text, ".md": parse_text, ".eml": parse_text,
    ".json": parse_json,
}


def _sniff(data: bytes):
    if data[:5] == b"%PDF-":
        return parse_pdf
    if data[:4] == b"PK\x03\x04":
        # zip container: xlsx or docx
        if b"xl/" in data[:4000] or b"xl/workbook" in data:
            return parse_xlsx
        if b"word/" in data[:4000] or b"word/document" in data:
            return parse_docx
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return parse_xls
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return parse_image
    return None


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Return (text, parser_name). Never raises: failures come back as empty text."""
    ext = Path(filename).suffix.lower()
    fn = _EXT.get(ext) or _sniff(data) or parse_text
    try:
        return fn(data), fn.__name__.replace("parse_", "")
    except Exception as e:  # corrupt file, missing OCR engine, ...
        # one more try with the sniffed parser if the extension lied
        alt = _sniff(data)
        if alt and alt is not fn:
            try:
                return alt(data), alt.__name__.replace("parse_", "")
            except Exception:
                pass
        return "", f"failed:{fn.__name__}:{type(e).__name__}"
