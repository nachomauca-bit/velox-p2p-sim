"""SQLAlchemy models.

Mock ERP tables are named after Dynamics 365 Finance *concepts* (plain names, not D365 entity
API names). Every mock-ERP row carries a `scenario` column: "asis" | "tobe". Business keys
(account_id, po_number, ...) are unique per scenario; a surrogate integer `id` is the primary key.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------------
# Mock ERP (system of record) — one copy per scenario
# --------------------------------------------------------------------------------------------


class LegalEntity(Base):
    __tablename__ = "legal_entity"
    __table_args__ = (UniqueConstraint("scenario", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    code: Mapped[str] = mapped_column(String(8))  # VDE | VFR | VUS
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))
    vat_id: Mapped[str] = mapped_column(String(40))
    address: Mapped[str] = mapped_column(String(200))


class Party(Base):
    """Global address book concept: one row per real supplier."""

    __tablename__ = "party"
    __table_args__ = (UniqueConstraint("scenario", "party_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    party_id: Mapped[str] = mapped_column(String(12))  # P-0001
    canonical_name: Mapped[str] = mapped_column(String(120))
    vat_id: Mapped[Optional[str]] = mapped_column(String(40))
    country: Mapped[str] = mapped_column(String(2))
    supplier_type: Mapped[str] = mapped_column(String(80))
    # Reference payment terms agreed with the supplier (contract or supplier agreement).
    agreed_terms_days: Mapped[int] = mapped_column(Integer)


class VendorAccount(Base):
    __tablename__ = "vendor_account"
    __table_args__ = (UniqueConstraint("scenario", "account_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    account_id: Mapped[str] = mapped_column(String(12))  # V-000123
    legal_entity_code: Mapped[str] = mapped_column(String(8))
    party_id: Mapped[Optional[str]] = mapped_column(String(12))  # nullable in asis (unlinked duplicates)
    display_name: Mapped[str] = mapped_column(String(120))
    vat_id: Mapped[Optional[str]] = mapped_column(String(40))
    iban: Mapped[Optional[str]] = mapped_column(String(64))
    payment_terms_days: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(40))
    created_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | inactive
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Which dirty-world rules produced/changed this row (asis only), e.g. ["D1", "D4"].
    corruption_rules: Mapped[list[str]] = mapped_column(JSON, default=list)


class Contract(Base):
    __tablename__ = "contract"
    __table_args__ = (UniqueConstraint("scenario", "contract_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    contract_id: Mapped[str] = mapped_column(String(20))
    party_id: Mapped[str] = mapped_column(String(12))
    legal_entity_code: Mapped[str] = mapped_column(String(8))
    description: Mapped[str] = mapped_column(String(200))
    payment_terms_days: Mapped[int] = mapped_column(Integer)
    recurring: Mapped[bool] = mapped_column(Boolean)
    expected_monthly_min: Mapped[float] = mapped_column(Float)
    expected_monthly_max: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    category: Mapped[str] = mapped_column(String(40))
    owner_name: Mapped[str] = mapped_column(String(80))
    owner_email: Mapped[str] = mapped_column(String(120))


class PurchaseOrder(Base):
    __tablename__ = "purchase_order"
    __table_args__ = (UniqueConstraint("scenario", "po_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    po_number: Mapped[str] = mapped_column(String(20))
    legal_entity_code: Mapped[str] = mapped_column(String(8))
    vendor_account_id: Mapped[str] = mapped_column(String(12))
    requester_name: Mapped[str] = mapped_column(String(80))
    requester_email: Mapped[str] = mapped_column(String(120))
    cost_centre: Mapped[str] = mapped_column(String(20))
    buyer_name: Mapped[str] = mapped_column(String(80))
    buyer_email: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(10))  # goods | service
    status: Mapped[str] = mapped_column(String(12), default="confirmed")
    order_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(200))
    total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))

    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan", order_by="PurchaseOrderLine.line_no"
    )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_order.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200))
    qty: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    receipt_required: Mapped[bool] = mapped_column(Boolean, default=True)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class ProductReceipt(Base):
    """Product receipt per PO line. Service lines use kind = service_confirmation, qty_received = 1."""

    __tablename__ = "product_receipt"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    receipt_id: Mapped[str] = mapped_column(String(20))  # one receipt document may cover several lines
    po_number: Mapped[str] = mapped_column(String(20))
    line_no: Mapped[int] = mapped_column(Integer)
    qty_received: Mapped[float] = mapped_column(Float)
    received_on: Mapped[date] = mapped_column(Date)
    received_by: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24), default="product_receipt")  # product_receipt | service_confirmation


class PendingVendorInvoice(Base):
    """The 'posted' side of the simulator (filled by the phase-2 gate)."""

    __tablename__ = "pending_vendor_invoice"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    invoice_id: Mapped[str] = mapped_column(String(20))
    doc_id: Mapped[Optional[str]] = mapped_column(String(20))
    legal_entity_code: Mapped[str] = mapped_column(String(8))
    vendor_account_id: Mapped[str] = mapped_column(String(12))
    invoice_number: Mapped[str] = mapped_column(String(40))
    invoice_date: Mapped[Optional[date]] = mapped_column(Date)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    terms_days: Mapped[Optional[int]] = mapped_column(Integer)
    terms_source: Mapped[str] = mapped_column(String(10))  # master | invoice
    po_number: Mapped[Optional[str]] = mapped_column(String(20))
    posted_on: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="pending_payment")
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CreditNoteApplication(Base):
    __tablename__ = "credit_note_application"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    credit_note_id: Mapped[str] = mapped_column(String(20))
    doc_id: Mapped[Optional[str]] = mapped_column(String(20))
    applied_to_invoice_id: Mapped[Optional[str]] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10))  # applied | unapplied


# --------------------------------------------------------------------------------------------
# Simulator tables
# --------------------------------------------------------------------------------------------


class InboundDocument(Base):
    __tablename__ = "inbound_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. B-01 (scenario letter + sample no)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    sample_no: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(16))  # ap_mailbox | store_mailbox
    mailbox: Mapped[str] = mapped_column(String(120))  # ap@velox.com | store.berlin01@velox.com
    received_on: Mapped[datetime] = mapped_column(DateTime)
    file_path: Mapped[str] = mapped_column(String(300))  # relative to project root
    file_hash: Mapped[str] = mapped_column(String(64))
    sender_email: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200))
    registered: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_on: Mapped[Optional[datetime]] = mapped_column(DateTime)
    doc_type: Mapped[str] = mapped_column(String(12), default="unknown")  # invoice | credit_note | unknown

    extraction: Mapped[Optional["Extraction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", uselist=False
    )


class Extraction(Base):
    __tablename__ = "extraction"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("inbound_document.doc_id", ondelete="CASCADE"), unique=True)
    model: Mapped[str] = mapped_column(String(80))
    json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_on: Mapped[datetime] = mapped_column(DateTime)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)

    document: Mapped[InboundDocument] = relationship(back_populates="extraction")


class GateDecision(Base):
    __tablename__ = "gate_decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(20), index=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{step, result, detail}]
    outcome: Mapped[str] = mapped_column(String(20))  # posted | exception | blocked_duplicate | applied_credit | human_review
    exception_type: Mapped[Optional[str]] = mapped_column(String(40))
    owner_role: Mapped[Optional[str]] = mapped_column(String(80))
    owner_name: Mapped[Optional[str]] = mapped_column(String(80))
    sla_days: Mapped[Optional[int]] = mapped_column(Integer)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    simulated_days: Mapped[Optional[float]] = mapped_column(Float)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decided_on: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True)
    scenario: Mapped[str] = mapped_column(String(8), index=True)
    started_on: Mapped[datetime] = mapped_column(DateTime)
    finished_on: Mapped[Optional[datetime]] = mapped_column(DateTime)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
