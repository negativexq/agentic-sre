"""Repositories for workload-owned PostgreSQL tables."""

from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from workload.common.contracts import (
    OrderCreateRequest,
    OrderResponse,
    OrderStatus,
    PaymentRequest,
    PaymentResponse,
    PaymentStatus,
)
from workload.common.models import OrderRow, PaymentRow

PAYMENT_NAMESPACE = UUID("8f04d61b-9d9e-4acb-8b7f-9a12d507e5d6")


class OrderRepository:
    """Persistence operations for orders."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, order_id: UUID, request: OrderCreateRequest, created_at: datetime
    ) -> OrderResponse:
        """Create a pending order."""
        row = OrderRow(
            order_id=order_id,
            customer_id=request.customer_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            status=OrderStatus.PENDING.value,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.commit()
        return self._to_response(row)

    def get(self, order_id: UUID) -> OrderResponse | None:
        """Get an order by identifier."""
        row = self._session.get(OrderRow, order_id)
        return None if row is None else self._to_response(row)

    def mark_paid(self, order_id: UUID, payment_id: UUID) -> OrderResponse:
        """Mark an order as paid."""
        row = self._required(order_id)
        row.status = OrderStatus.PAID.value
        row.payment_id = payment_id
        self._session.commit()
        return self._to_response(row)

    def mark_payment_failed(self, order_id: UUID) -> OrderResponse:
        """Mark an order as payment failed."""
        row = self._required(order_id)
        row.status = OrderStatus.PAYMENT_FAILED.value
        self._session.commit()
        return self._to_response(row)

    def _required(self, order_id: UUID) -> OrderRow:
        row = self._session.get(OrderRow, order_id)
        if row is None:
            raise LookupError(f"order {order_id} was not found")
        return row

    @staticmethod
    def _to_response(row: OrderRow) -> OrderResponse:
        return OrderResponse(
            order_id=row.order_id,
            customer_id=row.customer_id,
            amount_cents=row.amount_cents,
            currency=row.currency,
            status=OrderStatus(row.status),
            payment_id=row.payment_id,
        )


class PaymentRepository:
    """Persistence operations for payments."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, request: PaymentRequest, status: PaymentStatus, created_at: datetime
    ) -> PaymentResponse:
        """Create one deterministic payment record per order."""
        payment_id = uuid5(
            PAYMENT_NAMESPACE,
            f"{request.order_id}:{request.amount_cents}:{request.currency}",
        )
        row = PaymentRow(
            payment_id=payment_id,
            order_id=request.order_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            status=status.value,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.commit()
        return PaymentResponse(
            payment_id=row.payment_id,
            order_id=row.order_id,
            status=PaymentStatus(row.status),
        )

    def get_by_order(self, order_id: UUID) -> PaymentResponse | None:
        """Get a payment by its order identifier."""
        row = self._session.scalar(select(PaymentRow).where(PaymentRow.order_id == order_id))
        if row is None:
            return None
        return PaymentResponse(
            payment_id=row.payment_id,
            order_id=row.order_id,
            status=PaymentStatus(row.status),
        )
