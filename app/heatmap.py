from __future__ import annotations
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import EventModel, VisitorSession
from app.models import HeatmapResponse, HeatmapZone


def compute_heatmap(store_id: str, db: Session) -> HeatmapResponse:
    # visit_count: ZONE_ENTER events per zone, non-staff
    visit_rows = (
        db.query(EventModel.zone_id, func.count(EventModel.event_id))
        .filter(
            EventModel.store_id == store_id,
            EventModel.is_staff == False,
            EventModel.event_type == "ZONE_ENTER",
            EventModel.zone_id != None,
        )
        .group_by(EventModel.zone_id)
        .all()
    )

    # avg_dwell_ms: ZONE_DWELL events per zone, non-staff
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
    dwell_map = {row[0]: row[1] or 0.0 for row in dwell_rows if row[0]}

    if not visit_rows:
        # data_confidence based on session count
        customer_sessions = (
            db.query(func.count(VisitorSession.session_id))
            .filter(
                VisitorSession.store_id == store_id,
                VisitorSession.is_staff == False,
            )
            .scalar()
        ) or 0
        confidence = "LOW" if customer_sessions < 20 else "OK"
        return HeatmapResponse(store_id=store_id, data_confidence=confidence, zones=[])

    visit_dict = {row[0]: row[1] for row in visit_rows if row[0]}
    max_visits = max(visit_dict.values()) if visit_dict else 1

    # customer session count for confidence
    customer_sessions = (
        db.query(func.count(VisitorSession.session_id))
        .filter(
            VisitorSession.store_id == store_id,
            VisitorSession.is_staff == False,
        )
        .scalar()
    ) or 0
    confidence = "LOW" if customer_sessions < 20 else "OK"

    zones = []
    for zone_id, visit_count in sorted(visit_dict.items(), key=lambda x: -x[1]):
        avg_dwell = dwell_map.get(zone_id, 0.0)
        normalized = int(100 * visit_count / max_visits) if max_visits > 0 else 0
        zones.append(HeatmapZone(
            zone_id=zone_id,
            visit_count=visit_count,
            avg_dwell_ms=round(avg_dwell, 2),
            normalized_score=normalized,
        ))

    return HeatmapResponse(store_id=store_id, data_confidence=confidence, zones=zones)
