from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import EventModel, PosTransaction
from app.models import AnomaliesResponse, AnomalyItem, Severity

BILLING_QUEUE_WARN_THRESHOLD = 5
BILLING_QUEUE_CRITICAL_THRESHOLD = 8
STALE_FEED_MINUTES = 10
DEAD_ZONE_MINUTES = 30
CONVERSION_DROP_MIN_SESSIONS = 14  # need at least 2 weeks of daily data


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _check_billing_queue_spike(store_id: str, db: Session) -> Optional[AnomalyItem]:
    """Detect high billing queue depth from latest billing event metadata."""
    row = (
        db.query(EventModel.metadata_json, EventModel.timestamp)
        .filter(
            EventModel.store_id == store_id,
            EventModel.event_type == "BILLING_QUEUE_JOIN",
        )
        .order_by(EventModel.timestamp.desc())
        .first()
    )
    if not row:
        return None

    try:
        meta = json.loads(row[0] or "{}")
        queue_depth = meta.get("queue_depth")
        if queue_depth is None:
            return None
        queue_depth = int(queue_depth)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

    if queue_depth >= BILLING_QUEUE_CRITICAL_THRESHOLD:
        severity = Severity.CRITICAL
        msg = f"Billing queue depth is critically high: {queue_depth} customers waiting."
    elif queue_depth >= BILLING_QUEUE_WARN_THRESHOLD:
        severity = Severity.WARN
        msg = f"Billing queue depth elevated: {queue_depth} customers waiting."
    else:
        return None

    return AnomalyItem(
        type="BILLING_QUEUE_SPIKE",
        severity=severity,
        message=msg,
        suggested_action="Open another billing counter or assign staff to checkout.",
        detected_at=datetime.now(timezone.utc),
    )


def _check_conversion_drop(store_id: str, db: Session) -> Optional[AnomalyItem]:
    """Detect significant conversion rate drop vs 7-day baseline."""
    # Count distinct days with POS data
    from sqlalchemy import cast, Date
    daily_counts = (
        db.query(func.count(PosTransaction.transaction_id))
        .filter(PosTransaction.store_id == store_id)
        .scalar()
    ) or 0

    if daily_counts < 5:
        return AnomalyItem(
            type="CONVERSION_DROP",
            severity=Severity.INFO,
            message="Insufficient historical baseline for conversion drop detection.",
            suggested_action="Collect more baseline data or compare against store-level weekly average.",
            detected_at=datetime.now(timezone.utc),
        )

    # Compare recent 1-day POS vs 7-day average
    now = _utc_now()
    one_day_ago = now - timedelta(days=1)
    seven_days_ago = now - timedelta(days=7)

    recent_purchases = (
        db.query(func.count(PosTransaction.transaction_id))
        .filter(
            PosTransaction.store_id == store_id,
            PosTransaction.timestamp >= one_day_ago,
        )
        .scalar()
    ) or 0

    historical_purchases = (
        db.query(func.count(PosTransaction.transaction_id))
        .filter(
            PosTransaction.store_id == store_id,
            PosTransaction.timestamp >= seven_days_ago,
            PosTransaction.timestamp < one_day_ago,
        )
        .scalar()
    ) or 0

    if historical_purchases == 0:
        return None

    daily_avg = historical_purchases / 6.0
    if daily_avg > 0 and recent_purchases < daily_avg * 0.5:
        return AnomalyItem(
            type="CONVERSION_DROP",
            severity=Severity.WARN,
            message=f"Today's purchases ({recent_purchases}) are significantly below 7-day daily average ({daily_avg:.1f}).",
            suggested_action="Review floor activity, promotions, or check if feed data is complete.",
            detected_at=datetime.now(timezone.utc),
        )
    return None


def _check_dead_zones(store_id: str, db: Session) -> list[AnomalyItem]:
    """Detect zones with no ZONE_ENTER events in last 30 minutes."""
    now = _utc_now()
    cutoff = now - timedelta(minutes=DEAD_ZONE_MINUTES)

    # All known zones from event history
    all_zones = set(
        row[0]
        for row in db.query(EventModel.zone_id)
        .filter(
            EventModel.store_id == store_id,
            EventModel.zone_id != None,
            EventModel.event_type == "ZONE_ENTER",
        )
        .distinct()
        .all()
        if row[0]
    )

    if not all_zones:
        return []

    # Zones active in last 30 min
    active_zones = set(
        row[0]
        for row in db.query(EventModel.zone_id)
        .filter(
            EventModel.store_id == store_id,
            EventModel.zone_id != None,
            EventModel.event_type == "ZONE_ENTER",
            EventModel.timestamp >= cutoff,
        )
        .distinct()
        .all()
        if row[0]
    )

    dead = all_zones - active_zones
    anomalies = []
    for zone in sorted(dead):
        anomalies.append(AnomalyItem(
            type="DEAD_ZONE",
            severity=Severity.WARN,
            message=f"Zone '{zone}' has had no visitor entries in the last {DEAD_ZONE_MINUTES} minutes.",
            suggested_action="Check product placement, signage, or camera coverage for this zone.",
            detected_at=datetime.now(timezone.utc),
        ))
    return anomalies


def _check_stale_feed(store_id: str, db: Session) -> Optional[AnomalyItem]:
    """Detect if the latest event is older than 10 minutes."""
    last_ts = (
        db.query(func.max(EventModel.timestamp))
        .filter(EventModel.store_id == store_id)
        .scalar()
    )

    if last_ts is None:
        return None

    now = _utc_now()
    lag = now - last_ts
    if lag > timedelta(minutes=STALE_FEED_MINUTES):
        minutes_ago = int(lag.total_seconds() / 60)
        return AnomalyItem(
            type="STALE_FEED",
            severity=Severity.WARN,
            message=f"Last event for store '{store_id}' was {minutes_ago} minutes ago. Feed may be stale.",
            suggested_action="Check camera feed, detection process, or event replay pipeline.",
            detected_at=datetime.now(timezone.utc),
        )
    return None


def detect_anomalies(store_id: str, db: Session) -> AnomaliesResponse:
    anomalies: list[AnomalyItem] = []

    spike = _check_billing_queue_spike(store_id, db)
    if spike:
        anomalies.append(spike)

    conversion = _check_conversion_drop(store_id, db)
    if conversion:
        anomalies.append(conversion)

    dead_zones = _check_dead_zones(store_id, db)
    anomalies.extend(dead_zones)

    stale = _check_stale_feed(store_id, db)
    if stale:
        anomalies.append(stale)

    return AnomaliesResponse(store_id=store_id, anomalies=anomalies)
