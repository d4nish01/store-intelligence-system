from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import EventModel, PosTransaction, VisitorSession
from app.models import MetricsResponse


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def compute_metrics(store_id: str, db: Session) -> MetricsResponse:
    # --- unique customer visitors ---
    unique_visitors_q = (
        db.query(func.count(func.distinct(EventModel.visitor_id)))
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type.in_(["ENTRY", "REENTRY", "ZONE_ENTER", "ZONE_DWELL"]),
        )
        .scalar()
    ) or 0

    # --- total entries (ENTRY events, non-staff, distinct visitor) ---
    total_entries = (
        db.query(func.count(EventModel.event_id))
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type == "ENTRY",
        )
        .scalar()
    ) or 0

    # --- total exits ---
    total_exits = (
        db.query(func.count(EventModel.event_id))
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type == "EXIT",
        )
        .scalar()
    ) or 0

    # --- avg dwell per zone (from ZONE_DWELL events, non-staff) ---
    dwell_rows = (
        db.query(EventModel.zone_id, func.avg(EventModel.dwell_ms))
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type == "ZONE_DWELL",
            EventModel.zone_id != None,
        )
        .group_by(EventModel.zone_id)
        .all()
    )
    avg_dwell_per_zone = {row[0]: round(row[1], 2) for row in dwell_rows if row[0]}

    # --- current queue depth: latest metadata.queue_depth from billing events ---
    latest_billing = (
        db.query(EventModel.metadata_json)
        .filter(
            EventModel.store_id == store_id,
            EventModel.event_type == "BILLING_QUEUE_JOIN",
        )
        .order_by(EventModel.timestamp.desc())
        .first()
    )
    current_queue_depth = 0
    if latest_billing:
        try:
            meta = json.loads(latest_billing[0] or "{}")
            qd = meta.get("queue_depth")
            if qd is not None:
                current_queue_depth = int(qd)
        except (json.JSONDecodeError, ValueError):
            pass

    # --- abandonment rate ---
    billing_sessions = (
        db.query(func.count(func.distinct(VisitorSession.visitor_id)))
        .filter(
            VisitorSession.store_id == store_id,
            VisitorSession.is_staff == False,
            VisitorSession.has_billing == True,
        )
        .scalar()
    ) or 0

    abandon_events = (
        db.query(func.count(func.distinct(EventModel.visitor_id)))
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type == "BILLING_QUEUE_ABANDON",
        )
        .scalar()
    ) or 0

    abandonment_rate = 0.0
    if billing_sessions > 0:
        abandonment_rate = round(min(abandon_events / billing_sessions, 1.0) * 100, 2)

    # --- purchases from POS ---
    purchases = (
        db.query(func.count(PosTransaction.transaction_id))
        .filter(PosTransaction.store_id == store_id)
        .scalar()
    ) or 0

    # --- conversion rate ---
    conversion_rate = 0.0
    if unique_visitors_q > 0 and purchases > 0:
        conversion_rate = round(purchases / unique_visitors_q * 100, 2)

    # --- last updated ---
    last_ts = (
        db.query(func.max(EventModel.timestamp))
        .filter(EventModel.store_id == store_id)
        .scalar()
    )
    last_updated: Optional[datetime] = None
    if last_ts:
        last_updated = last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc)

    return MetricsResponse(
        store_id=store_id,
        unique_visitors=unique_visitors_q,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        current_queue_depth=current_queue_depth,
        abandonment_rate=abandonment_rate,
        total_entries=total_entries,
        total_exits=total_exits,
        purchases=purchases,
        last_updated=last_updated,
    )
