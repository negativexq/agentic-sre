"""Normalized infrastructure change contract."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from packages.contracts.models import ContractModel


class ChangeType(StrEnum):
    """Observed deployment/resource change categories."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    ROLLOUT = "ROLLOUT"
    SCALED = "SCALED"


class ChangeRecord(ContractModel):
    """Captured fact about a resource change; no inferred cause."""

    change_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    resource_type: str = Field(min_length=1)
    resource_name: str = Field(min_length=1)
    change_type: ChangeType
    before: dict[str, Any]
    after: dict[str, Any]
    revision: str = Field(min_length=1)
    source: str = Field(min_length=1)
