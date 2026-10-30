"""Sanity checks on an extracted document. Catches wrong extractions before they are trusted."""
from __future__ import annotations

from .schema import Document


def _close(a: float, b: float, tol: float = 0.02) -> bool:
    return abs(a - b) <= max(1.0, abs(b) * tol)


def check(doc: Document) -> list[str]:
    issues: list[str] = []
    if doc.doc_type == "unknown":
        issues.append("Could not tell whether this is a quotation or a purchase order")
    if not doc.doc_number:
        issues.append("Missing document number")
    if not doc.line_items:
        issues.append("No line items found")
    if doc.grand_total is None:
        issues.append("Missing grand total")

    for i, li in enumerate(doc.line_items, 1):
        if li.quantity is not None and li.unit_price is not None and li.line_total is not None:
            if not _close(li.quantity * li.unit_price, li.line_total, 0.2):
                # tax-inclusive totals are common, so only flag large gaps
                issues.append(f"Line {i}: qty x price does not match line total")

    totals = [li.line_total for li in doc.line_items if li.line_total is not None]
    if totals and doc.subtotal is not None and not _close(sum(totals), doc.subtotal):
        issues.append(f"Line totals ({sum(totals):.2f}) do not add up to subtotal ({doc.subtotal:.2f})")
    if doc.subtotal is not None and doc.grand_total is not None:
        expected = doc.subtotal + (doc.tax_total or 0)
        if not _close(expected, doc.grand_total):
            issues.append(f"Subtotal + tax ({expected:.2f}) does not match grand total ({doc.grand_total:.2f})")
    return issues


def score(doc: Document, issues: list[str], base: float) -> float:
    """Combine extractor confidence with rule results and field completeness."""
    fields = [doc.doc_number, doc.doc_date, doc.vendor.name, doc.buyer.name, doc.grand_total, doc.currency]
    completeness = sum(f is not None and f != "" for f in fields) / len(fields)
    s = 0.5 * base + 0.3 * completeness + 0.2
    s -= 0.12 * len(issues)
    return round(max(0.0, min(1.0, s)), 2)
