"""SQLAlchemy schema for the production-like workload databases."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from packages.storage.models import UTCDateTime


class WorkloadBase(DeclarativeBase):
    """Declarative metadata root for workload-owned tables."""


class OrderRow(WorkloadBase):
    """Order persistence row."""

    __tablename__ = "orders"

    order_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PaymentRow(WorkloadBase):
    """Payment persistence row."""

    __tablename__ = "payments"

    payment_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
