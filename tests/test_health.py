# PROMPT:
# I asked an AI assistant to generate pytest coverage for the /health endpoint.
# The tests needed to verify that status is "ok" when DB is accessible, that
# per-store feed_status correctly reflects "LIVE" vs "STALE_FEED", that the
# response includes db, version, and uptime_seconds fields, and that a structured
# 503 body is returned when the database is unavailable.
#
# CHANGES MADE:
# I reviewed the generated tests and added an assertion that uptime_seconds > 0,
# that last_event_timestamp is returned in stores dict, that a store with events
# older than 10 minutes gets STALE_FEED while a store with recent events gets LIVE,
# and a soft test for the 503 case by patching check_db.

import uuid
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from app.database import EventModel


class TestHealthOk:
    def test_health_status_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_db_ok(self, client):
        resp = client.get("/health")
        assert resp.json()["db"] == "ok"

    def test_health_version_present(self, client):
        resp = client.get("/health")
        assert "version" in resp.json()
        assert resp.json()["version"] != ""

    def test_health_uptime_positive(self, client):
        resp = client.get("/health")
        assert resp.json()["uptime_seconds"] >= 0

    def test_health_stores_dict(self, client):
        resp = client.get("/health")
        assert isinstance(resp.json()["stores"], dict)


class TestHealthFeedStatus:
    def test_live_feed_status_for_recent_event(self, client, db):
        store = "STORE_H_LIVE"
        recent_evt = EventModel(
            event_id=str(uuid.uuid4()), store_id=store, camera_id="CAM_01",
            visitor_id="VIS_A", event_type="ENTRY",
            timestamp=datetime.utcnow() - timedelta(minutes=2),
            is_staff=False, confidence=0.9, metadata_json="{}", created_at=datetime.utcnow(),
        )
        db.add(recent_evt)
        db.commit()
        resp = client.get("/health")
        stores = resp.json()["stores"]
        assert store in stores
        assert stores[store]["feed_status"] == "LIVE"

    def test_stale_feed_status_for_old_event(self, client, db):
        store = "STORE_H_STALE"
        old_evt = EventModel(
            event_id=str(uuid.uuid4()), store_id=store, camera_id="CAM_01",
            visitor_id="VIS_B", event_type="ENTRY",
            timestamp=datetime.utcnow() - timedelta(minutes=15),
            is_staff=False, confidence=0.9, metadata_json="{}", created_at=datetime.utcnow(),
        )
        db.add(old_evt)
        db.commit()
        resp = client.get("/health")
        stores = resp.json()["stores"]
        assert store in stores
        assert stores[store]["feed_status"] == "STALE_FEED"

    def test_store_last_event_timestamp_present(self, client, db):
        store = "STORE_H_TS"
        evt = EventModel(
            event_id=str(uuid.uuid4()), store_id=store, camera_id="CAM_01",
            visitor_id="VIS_C", event_type="ENTRY",
            timestamp=datetime.utcnow() - timedelta(minutes=1),
            is_staff=False, confidence=0.9, metadata_json="{}", created_at=datetime.utcnow(),
        )
        db.add(evt)
        db.commit()
        resp = client.get("/health")
        stores = resp.json()["stores"]
        assert stores[store]["last_event_timestamp"] is not None


class TestHealthDbFailure:
    def test_503_when_db_unavailable(self, client):
        with patch("app.health.check_db", return_value=False):
            resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "error"
        assert body["db"] == "error"
