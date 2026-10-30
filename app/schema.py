"""The ONE standard format every quotation / purchase order is converted into."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Party(BaseModel):
    name: Optional[str] = None
    tax_id: Optional[str] = Field(None, description="GSTIN / VAT / tax number")
    address: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class LineItem(BaseModel):
    description: str = ""
    item_code: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    tax_percent: Optional[float] = None
    line_total: Optional[float] = None


class Document(BaseModel):
    doc_type: Literal["quotation", "purchase_order", "unknown"] = "unknown"
    doc_number: Optional[str] = None
    doc_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    valid_until_or_delivery_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    reference_number: Optional[str] = Field(
        None, description="For a PO: the quotation number it was raised against (and vice versa)"
    )
    currency: Optional[str] = Field(None, description="ISO code such as INR, USD")
    payment_terms: Optional[str] = None
    vendor: Party = Field(default_factory=Party, description="The party SELLING / supplying")
    buyer: Party = Field(default_factory=Party, description="The party BUYING / ordering")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Optional[float] = None
    tax_total: Optional[float] = None
    grand_total: Optional[float] = None
    notes: Optional[str] = None


class Extraction(BaseModel):
    """What an extractor returns: the document plus how sure it is."""

    document: Document
    confidence: float = 0.0
    issues: list[str] = Field(default_factory=list)
    method: str = "rules"
