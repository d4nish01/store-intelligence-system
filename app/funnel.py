from __future__ import annotations
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import EventModel, PosTransaction, VisitorSession
from app.models import FunnelResponse, FunnelStage

BILLING_EVENT_TYPES = {"BILLING_QUEUE_JOIN"}
BILLING_ZONE_NAMES = {"BILLING", "CHECKOUT", "CASHIER", "PAYMENT"}


def _dropoff(prev: int, curr: int) -> float:
    if prev == 0:
        return 0.0
    return round((prev - curr) / prev * 100, 2)


def compute_funnel(store_id: str, db: Session) -> FunnelResponse:
    # Stage 1: ENTRY – unique non-staff visitor_ids with ENTRY or REENTRY
    entry_visitors = set(
        row[0]
        for row in db.query(EventModel.visitor_id)
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type.in_(["ENTRY", "REENTRY"]),
        )
        .distinct()
        .all()
    )
    # De-dupe: only count visitor once even if REENTRY occurred
    entry_count = len(entry_visitors)

    # Stage 2: ZONE_VISIT – non-staff visitors who entered any zone
    zone_visitors = set(
        row[0]
        for row in db.query(EventModel.visitor_id)
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
        )
        .distinct()
        .all()
    )
    zone_count = len(zone_visitors)

    # Stage 3: BILLING_QUEUE – visitors who joined billing queue or visited billing zone
    billing_event_visitors = set(
        row[0]
        for row in db.query(EventModel.visitor_id)
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type == "BILLING_QUEUE_JOIN",
        )
        .distinct()
        .all()
    )
    billing_zone_visitors = set(
        row[0]
        for row in db.query(EventModel.visitor_id)
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.zone_id.in_(list(BILLING_ZONE_NAMES)),
        )
        .distinct()
        .all()
    )
    billing_visitors = billing_event_visitors | billing_zone_visitors
    billing_count = len(billing_visitors)

    # Stage 4: PURCHASE from POS transactions
    purchase_count = (
        db.query(func.count(PosTransaction.transaction_id))
        .filter(PosTransaction.store_id == store_id)
        .scalar()
    ) or 0

    stages = [
        FunnelStage(stage="ENTRY", count=entry_count, dropoff_from_previous_pct=0.0),
        FunnelStage(stage="ZONE_VISIT", count=zone_count, dropoff_from_previous_pct=_dropoff(entry_count, zone_count)),
        FunnelStage(stage="BILLING_QUEUE", count=billing_count, dropoff_from_previous_pct=_dropoff(zone_count, billing_count)),
        FunnelStage(stage="PURCHASE", count=purchase_count, dropoff_from_previous_pct=_dropoff(billing_count, purchase_count)),
    ]

    return FunnelResponse(store_id=store_id, stages=stages)
