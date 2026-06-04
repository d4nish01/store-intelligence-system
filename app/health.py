from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import __version__
from app.database import EventModel, check_db
from app.models import HealthResponse, StoreHealth

STALE_FEED_MINUTES = 10
_start_time = time.time()


def get_health(db: Session) -> tuple[HealthResponse, int]:
    db_ok = check_db()
    uptime = time.time() - _start_time

    if not db_ok:
        return HealthResponse(
            status="error",
            db="error",
            version=__version__,
            uptime_seconds=round(uptime, 2),
            stores={},
        ), 503

    # Collect per-store last event timestamp
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_cutoff = now - timedelta(minutes=STALE_FEED_MINUTES)

    store_rows = (
        db.query(EventModel.store_id, func.max(EventModel.timestamp))
        .group_by(EventModel.store_id)
        .all()
    )

    stores: dict[str, StoreHealth] = {}
    for store_id, last_ts in store_rows:
        if last_ts is None:
            feed_status = "UNKNOWN"
        elif last_ts < stale_cutoff:
            feed_status = "STALE_FEED"
        else:
            feed_status = "LIVE"

        last_event = last_ts.replace(tzinfo=timezone.utc) if last_ts else None
        stores[store_id] = StoreHealth(
            last_event_timestamp=last_event,
            feed_status=feed_status,
        )

    return HealthResponse(
        status="ok",
        db="ok",
        version=__version__,
        uptime_seconds=round(uptime, 2),
        stores=stores,
    ), 200
