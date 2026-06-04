# PROMPT:
# I asked an AI assistant to generate pytest coverage for the /stores/{id}/anomalies endpoint.
# The tests needed to verify BILLING_QUEUE_SPIKE detection at WARN and CRITICAL thresholds,
# STALE_FEED detection when the last event is older than 10 minutes, CONVERSION_DROP returning
# an INFO anomaly when insufficient historical data exists, DEAD_ZONE detection after 30 minutes
# of zone inactivity, and that every anomaly returned includes a non-empty suggested_action.
#
# CHANGES MADE:
# I reviewed the generated tests and added threshold boundary tests (queue_depth=4 → no spike,
# queue_depth=5 → WARN, queue_depth=8 → CRITICAL), asserted that every anomaly has a
# detected_at timestamp, and added a test confirming that a store with recent events
# does NOT trigger STALE_FEED.

import uuid
import pytest
from datetime import datetime, timezone, timedelta

from tests.conftest import make_event
from app.database import EventModel


class TestBillingQueueSpike:
    def test_no_spike_below_threshold(self, client):
        store = "STORE_AN_QD_LOW"
        evt = make_event(store_id=store, visitor_id="VIS_A", event_type="BILLING_QUEUE_JOIN",
                         zone_id="BILLING", queue_depth=4)
        client.post("/events/ingest", json={"events": [evt]})
        resp = client.get(f"/stores/{store}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 0

    def test_warn_at_threshold_5(self, client):
        store = "STORE_AN_QD_WARN"
        evt = make_event(store_id=store, visitor_id="VIS_A", event_type="BILLING_QUEUE_JOIN",
                         zone_id="BILLING", queue_depth=5)
        client.post("/events/ingest", json={"events": [evt]})
        resp = client.get(f"/stores/{store}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 1
        assert spikes[0]["severity"] == "WARN"

    def test_critical_at_threshold_8(self, client):
        store = "STORE_AN_QD_CRIT"
        evt = make_event(store_id=store, visitor_id="VIS_A", event_type="BILLING_QUEUE_JOIN",
                         zone_id="BILLING", queue_depth=8)
        client.post("/events/ingest", json={"events": [evt]})
        resp = client.get(f"/stores/{store}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 1
        assert spikes[0]["severity"] == "CRITICAL"

    def test_queue_spike_has_suggested_action(self, client):
        store = "STORE_AN_QD_ACTION"
        evt = make_event(store_id=store, visitor_id="VIS_A", event_type="BILLING_QUEUE_JOIN",
                         zone_id="BILLING", queue_depth=6)
        client.post("/events/ingest", json={"events": [evt]})
        resp = client.get(f"/stores/{store}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
        assert spikes[0]["suggested_action"] != ""


class TestStaleFeed:
    def test_stale_feed_detected_when_last_event_old(self, client, db):
        store = "STORE_AN_STALE"
        old_time = datetime.utcnow() - timedelta(minutes=15)
        old_evt = EventModel(
            event_id=str(uuid.uuid4()),
            store_id=store,
            camera_id="CAM_01",
            visitor_id="VIS_A",
            event_type="ENTRY",
            timestamp=old_time,
            is_staff=False,
            confidence=0.9,
            metadata_json="{}",
            created_at=datetime.utcnow(),
        )
        db.add(old_evt)
        db.commit()
        resp = client.get(f"/stores/{store}/anomalies")
        stale = [a for a in resp.json()["anomalies"] if a["type"] == "STALE_FEED"]
        assert len(stale) == 1
        assert stale[0]["severity"] == "WARN"

    def test_no_stale_feed_for_recent_events(self, client):
        store = "STORE_AN_FRESH"
        evt = make_event(store_id=store)  # timestamp = now
        client.post("/events/ingest", json={"events": [evt]})
        resp = client.get(f"/stores/{store}/anomalies")
        stale = [a for a in resp.json()["anomalies"] if a["type"] == "STALE_FEED"]
        assert len(stale) == 0

    def test_stale_feed_has_suggested_action(self, client, db):
        store = "STORE_AN_STALE_ACT"
        old_time = datetime.utcnow() - timedelta(minutes=20)
        old_evt = EventModel(
            event_id=str(uuid.uuid4()), store_id=store, camera_id="CAM_01",
            visitor_id="VIS_B", event_type="ENTRY", timestamp=old_time,
            is_staff=False, confidence=0.9, metadata_json="{}", created_at=datetime.utcnow(),
        )
        db.add(old_evt)
        db.commit()
        resp = client.get(f"/stores/{store}/anomalies")
        stale = [a for a in resp.json()["anomalies"] if a["type"] == "STALE_FEED"]
        assert stale[0]["suggested_action"] != ""


class TestConversionDrop:
    def test_info_anomaly_when_insufficient_historical_data(self, client):
        store = "STORE_AN_CONV_INFO"
        resp = client.get(f"/stores/{store}/anomalies")
        conv = [a for a in resp.json()["anomalies"] if a["type"] == "CONVERSION_DROP"]
        assert len(conv) == 1
        assert conv[0]["severity"] == "INFO"

    def test_conversion_anomaly_has_suggested_action(self, client):
        store = "STORE_AN_CONV_ACT"
        resp = client.get(f"/stores/{store}/anomalies")
        conv = [a for a in resp.json()["anomalies"] if a["type"] == "CONVERSION_DROP"]
        assert conv[0]["suggested_action"] != ""


class TestDeadZone:
    def test_dead_zone_detected_when_no_recent_entry(self, client, db):
        store = "STORE_AN_DEAD"
        old_time = datetime.utcnow() - timedelta(minutes=40)
        old_evt = EventModel(
            event_id=str(uuid.uuid4()), store_id=store, camera_id="CAM_01",
            visitor_id="VIS_A", event_type="ZONE_ENTER", timestamp=old_time,
            zone_id="SKINCARE", is_staff=False, confidence=0.9,
            metadata_json="{}", created_at=datetime.utcnow(),
        )
        db.add(old_evt)
        db.commit()
        resp = client.get(f"/stores/{store}/anomalies")
        dead = [a for a in resp.json()["anomalies"] if a["type"] == "DEAD_ZONE"]
        assert len(dead) == 1
        assert dead[0]["suggested_action"] != ""

    def test_no_dead_zone_for_recently_active_zone(self, client):
        store = "STORE_AN_ACTIVE"
        evt = make_event(store_id=store, event_type="ZONE_ENTER", zone_id="SKINCARE")
        client.post("/events/ingest", json={"events": [evt]})
        resp = client.get(f"/stores/{store}/anomalies")
        dead = [a for a in resp.json()["anomalies"] if a["type"] == "DEAD_ZONE"]
        assert len(dead) == 0


class TestAnomalyStructure:
    def test_all_anomalies_have_required_fields(self, client, db):
        store = "STORE_AN_STRUCT"
        old_time = datetime.utcnow() - timedelta(minutes=15)
        old_evt = EventModel(
            event_id=str(uuid.uuid4()), store_id=store, camera_id="CAM_01",
            visitor_id="VIS_A", event_type="BILLING_QUEUE_JOIN", timestamp=old_time,
            zone_id="BILLING", is_staff=False, confidence=0.9,
            metadata_json='{"queue_depth": 9, "sku_zone": null, "session_seq": 1}',
            created_at=datetime.utcnow(),
        )
        db.add(old_evt)
        db.commit()
        resp = client.get(f"/stores/{store}/anomalies")
        for anomaly in resp.json()["anomalies"]:
            assert "type" in anomaly
            assert "severity" in anomaly
            assert "message" in anomaly
            assert "suggested_action" in anomaly
            assert "detected_at" in anomaly
            assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")
            assert anomaly["suggested_action"] != ""
