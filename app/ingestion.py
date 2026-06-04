from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import EventModel, VisitorSession
from app.models import EventIn, ErrorItem, IngestResponse


BILLING_ZONES = {"BILLING", "CHECKOUT", "CASHIER", "PAYMENT"}


def _is_billing_event(event: EventIn) -> bool:
    if event.event_type.value == "BILLING_QUEUE_JOIN":
        return True
    if event.zone_id and event.zone_id.upper() in BILLING_ZONES:
        return True
    if event.metadata.sku_zone and event.metadata.sku_zone.upper() in {"CHECKOUT", "BILLING"}:
        return True
    return False


def _upsert_session(db: Session, event: EventIn) -> None:
    session_id = f"{event.store_id}:{event.visitor_id}"
    session = db.get(VisitorSession, session_id)

    ts = event.timestamp
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)

    if session is None:
        session = VisitorSession(
            session_id=session_id,
            store_id=event.store_id,
            visitor_id=event.visitor_id,
            first_seen=ts,
            last_seen=ts,
            has_entry=False,
            has_zone_visit=False,
            has_billing=False,
            has_purchase=False,
            is_staff=event.is_staff,
            reentry_count=0,
        )
        db.add(session)

    # update last_seen
    if session.last_seen is None or ts > session.last_seen:
        session.last_seen = ts
    if session.first_seen is None or ts < session.first_seen:
        session.first_seen = ts

    et = event.event_type.value
    if et == "ENTRY":
        session.has_entry = True
    elif et in ("ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"):
        session.has_zone_visit = True
    elif et == "REENTRY":
        session.reentry_count = (session.reentry_count or 0) + 1
        session.has_entry = True  # counts as in-store

    if _is_billing_event(event):
        session.has_billing = True

    if event.is_staff:
        session.is_staff = True


def ingest_events(raw_events: list[dict[str, Any]], db: Session) -> IngestResponse:
    accepted = 0
    duplicates = 0
    rejected = 0
    errors: list[ErrorItem] = []

    for idx, raw in enumerate(raw_events):
        event_id_hint = raw.get("event_id") if isinstance(raw, dict) else None

        # Validate
        try:
            event = EventIn.model_validate(raw)
        except ValidationError as exc:
            rejected += 1
            first_err = exc.errors()[0]
            errors.append(ErrorItem(
                index=idx,
                event_id=event_id_hint,
                field=".".join(str(x) for x in first_err.get("loc", [])) or None,
                message=first_err.get("msg", "Validation error"),
            ))
            continue
        except Exception as exc:
            rejected += 1
            errors.append(ErrorItem(index=idx, event_id=event_id_hint, message=str(exc)))
            continue

        # Deduplication check
        existing = db.get(EventModel, event.event_id)
        if existing is not None:
            duplicates += 1
            continue

        # Strip timezone for SQLite storage
        ts = event.timestamp
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)

        # Persist event
        db_event = EventModel(
            event_id=event.event_id,
            store_id=event.store_id,
            camera_id=event.camera_id,
            visitor_id=event.visitor_id,
            event_type=event.event_type.value,
            timestamp=ts,
            zone_id=event.zone_id,
            dwell_ms=event.dwell_ms,
            is_staff=event.is_staff,
            confidence=event.confidence,
            metadata_json=event.metadata.model_dump_json(),
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(db_event)

        # Update visitor session
        _upsert_session(db, event)
        db.flush()
        accepted += 1

    db.commit()

    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=errors,
    )
