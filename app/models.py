from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class EventIn(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        try:
            UUID(v, version=4)
        except (ValueError, AttributeError):
            raise ValueError(f"event_id must be a valid UUID v4, got: {v!r}")
        return v

    @field_validator("store_id", "camera_id", "visitor_id")
    @classmethod
    def non_empty_string(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must not be empty")
        return v


class EventBatchIn(BaseModel):
    events: list[dict[str, Any]] = Field(..., max_length=500)

    @model_validator(mode="before")
    @classmethod
    def check_batch_size(cls, values: dict) -> dict:
        events = values.get("events", [])
        if len(events) > 500:
            raise ValueError("Batch size must not exceed 500 events")
        return values


class ErrorItem(BaseModel):
    index: int
    event_id: Optional[str] = None
    field: Optional[str] = None
    message: str


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    errors: list[ErrorItem]


class ZoneDwellStat(BaseModel):
    zone_id: str
    avg_dwell_ms: float


class MetricsResponse(BaseModel):
    store_id: str
    unique_visitors: int = 0
    conversion_rate: float = 0.0
    avg_dwell_per_zone: dict[str, float] = {}
    current_queue_depth: int = 0
    abandonment_rate: float = 0.0
    total_entries: int = 0
    total_exits: int = 0
    purchases: int = 0
    last_updated: Optional[datetime] = None


class FunnelStage(BaseModel):
    stage: str
    count: int
    dropoff_from_previous_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage]


class HeatmapZone(BaseModel):
    zone_id: str
    visit_count: int
    avg_dwell_ms: float
    normalized_score: int


class HeatmapResponse(BaseModel):
    store_id: str
    data_confidence: str
    zones: list[HeatmapZone]


class AnomalyItem(BaseModel):
    type: str
    severity: Severity
    message: str
    suggested_action: str
    detected_at: datetime


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[AnomalyItem]


class StoreHealth(BaseModel):
    last_event_timestamp: Optional[datetime]
    feed_status: str


class HealthResponse(BaseModel):
    status: str
    db: str
    version: str
    uptime_seconds: float
    stores: dict[str, StoreHealth]
