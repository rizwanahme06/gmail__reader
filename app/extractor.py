"""Text -> standard Document.

Primary: Claude with a strict JSON schema (handles any layout / vendor).
Fallback: regex rules (works offline, lower accuracy) so the pipeline never stops.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import config
from .schema import Document, Extraction, LineItem, Party
from .validate import check, score

SYSTEM = (
    "You extract structured data from business quotations and purchase orders. "
    "Return ONLY a JSON object matching the schema. Use null for anything not stated; never guess. "
    "Dates must be ISO YYYY-MM-DD. Amounts are plain numbers (no currency symbols or thousands separators). "
    "'vendor' is the party selling/supplying; 'buyer' is the party purchasing. "
    "doc_type is 'purchase_order' if it instructs a supplier to deliver goods/services, "
    "'quotation' if it offers a price, else 'unknown'. "
    "Also include a top-level number 'confidence' from 0 to 1 for how reliable the extraction is."
)


def _llm(text: str, email_ctx: str) -> Extraction:
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    schema = json.dumps(Document.model_json_schema(), indent=1)
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"JSON schema:\n{schema}\n\nEmail context:\n{email_ctx}\n\nDocument text:\n{text}\n\nJSON:",
        }],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON in model output")
    data = json.loads(m.group(0))
    conf = float(data.pop("confidence", 0.7) or 0.7)
    doc = Document.model_validate(data)
    return Extraction(document=doc, confidence=conf, method="llm")


# ---------------- rule-based fallback ----------------

_NUM = r"([\d][\d,]*\.?\d*)"


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _find(pattern: str, text: str, flags=re.I) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1).strip(" |\t") or None if m else None


def _rules(text: str) -> Extraction:
    low = text.lower()
    if re.search(r"purchase\s*order|\bp\.?o\.?\s*(no|number|#)", low):
        dtype = "purchase_order"
    elif re.search(r"quotation|\bquote\b|proforma|estimate", low):
        dtype = "quotation"
    else:
        dtype = "unknown"

    cur = None
    if re.search(r"₹|\bINR\b|\bRs\.?\b", text):
        cur = "INR"
    elif re.search(r"\$|\bUSD\b", text):
        cur = "USD"
    elif re.search(r"€|\bEUR\b", text):
        cur = "EUR"
    elif re.search(r"£|\bGBP\b", text):
        cur = "GBP"

    doc_no = _find(r"(?:P\.?O\.?|purchase\s*order|quotation|quote|ref)\s*(?:no\.?|number|#)\s*[:\-|]?\s*([A-Z0-9][\w\-/]*)", text)
    date_pat = r"\s*[:\-|]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4})"
    doc_date = _date(_find(r"\bdate" + date_pat, text))
    valid = _date(_find(r"(?:valid\s*(?:until|till|upto)|delivery\s*date|due\s*date|expiry)" + date_pat, text))
    ref = _find(r"\b(?:against|ref(?:erence)?|your\s*quote)\s*(?:quotation|quote)?\s*(?:no\.?|number)?\s*[:\-|]?\s*(Q[\w\-/]*\d[\w\-/]*)", text)
    terms = _find(r"payment\s*terms?\s*[:\-|]?\s*(.+)", text)
    gst = re.findall(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z][A-Z\d]\b", text)

    vendor = Party(name=_find(r"(?:vendor|supplier|from|seller)\s*[:\-|]\s*(.+)", text), tax_id=gst[0] if gst else None)
    buyer = Party(name=_find(r"(?:buyer|bill\s*to|customer|ship\s*to|to)\s*[:\-|]\s*(.+)", text),
                  tax_id=gst[1] if len(gst) > 1 else None)

    items: list[LineItem] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        nums = [_num(c) for c in cells]
        if sum(n is not None for n in nums) >= 3:
            # first non-numeric cell = description; last 3 numbers = qty, price, total
            desc = next((c for c, n in zip(cells, nums) if n is None and c), "")
            if not desc or re.search(r"total|tax|gst|subtotal", desc, re.I):
                continue
            vals = [n for n in nums if n is not None]
            items.append(LineItem(description=desc, quantity=vals[-3], unit_price=vals[-2], line_total=vals[-1]))

    sub = _num(_find(r"sub\s*-?\s*total\s*[:\-|]?\s*(?:[₹$€£]|Rs\.?|INR|USD)?\s*" + _NUM, text))
    tax = _num(_find(r"(?:total\s*)?(?:tax|gst|vat)(?:\s*\(?[\d.]+%\)?)?\s*[:\-|]?\s*(?:[₹$€£]|Rs\.?|INR|USD)?\s*" + _NUM, text))
    grand = _num(_find(r"(?:grand\s*total|total\s*amount|total\s*payable|net\s*total)\s*[:\-|]?\s*(?:[₹$€£]|Rs\.?|INR|USD)?\s*" + _NUM, text))

    doc = Document(doc_type=dtype, doc_number=doc_no, doc_date=doc_date, valid_until_or_delivery_date=valid,
                   reference_number=ref, currency=cur, payment_terms=terms, vendor=vendor, buyer=buyer,
                   line_items=items, subtotal=sub, tax_total=tax, grand_total=grand)
    return Extraction(document=doc, confidence=0.45, method="rules")


# ---------------- public entry ----------------

def extract(text: str, email_ctx: str = "") -> Extraction:
    ex: Extraction | None = None
    err = None
    if config.ANTHROPIC_API_KEY and text.strip():
        try:
            ex = _llm(text, email_ctx)
        except Exception as e:  # network, bad JSON, schema mismatch -> fall back
            err = f"AI extraction failed ({type(e).__name__}); used rule-based fallback"
    if ex is None:
        ex = _rules(text)
        if err:
            ex.issues.append(err)
        elif not config.ANTHROPIC_API_KEY:
            ex.issues.append("No ANTHROPIC_API_KEY set; used rule-based extraction (lower accuracy)")
    ex.issues += check(ex.document)
    ex.confidence = score(ex.document, ex.issues, ex.confidence)
    return ex
