"""Typed contracts shared by the demo workload services."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class WorkloadModel(BaseModel):
    """JSON-safe workload contract base.

    HTTP JSON represents UUIDs as strings, so request models intentionally use
    Pydantic's JSON parsing rules while still rejecting unknown fields.
    """

    model_config = ConfigDict(extra="forbid")


class OrderStatus(StrEnum):
    """Order processing state."""

    PENDING = "PENDING"
    PAID = "PAID"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class PaymentStatus(StrEnum):
    """Payment authorization state."""

    APPROVED = "APPROVED"
    DECLINED = "DECLINED"


class OrderCreateRequest(WorkloadModel):
    """Input accepted by the order service."""

    customer_id: str = Field(min_length=1, max_length=255)
    amount_cents: int = Field(gt=0, le=10_000_000)
    currency: str = Field(min_length=3, max_length=3)


class PaymentRequest(WorkloadModel):
    """Input sent from order service to payment service."""

    order_id: UUID
    amount_cents: int = Field(gt=0, le=10_000_000)
    currency: str = Field(min_length=3, max_length=3)


class PaymentResponse(WorkloadModel):
    """Payment service result."""

    payment_id: UUID
    order_id: UUID
    status: PaymentStatus


class OrderResponse(WorkloadModel):
    """Order service response."""

    order_id: UUID
    customer_id: str
    amount_cents: int
    currency: str
    status: OrderStatus
    payment_id: UUID | None = None


class EventTopic(StrEnum):
    """Kafka topics used by the workload."""

    ORDERS_CREATED = "orders.created"


class OrderCreatedEvent(WorkloadModel):
    """Event consumed by the order worker."""

    event_id: UUID = Field(default_factory=uuid4)
    topic: EventTopic = EventTopic.ORDERS_CREATED
    order_id: UUID
    customer_id: str
    amount_cents: int
    currency: str
