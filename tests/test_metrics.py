# PROMPT:
# I asked an AI assistant to generate pytest coverage for the /stores/{id}/metrics endpoint.
# The tests needed to verify empty-store zero values, staff exclusion from unique visitor counts,
# zero-purchase conversion rate, POS-based purchase correlation, billing abandonment rate,
# and that REENTRY events don't inflate unique visitor counts.
#
# CHANGES MADE:
# I reviewed the generated tests and added explicit assertions that conversion_rate is 0.0
# when purchases are 0 (not null or missing), added a test that ingesting the same visitor
# with ENTRY + REENTRY counts as 1 unique visitor, and added a check that avg_dwell_per_zone
# is a dict (not null) even when no dwell events exist.

import uuid
import pytest
from datetime import datetime, timezone

from tests.conftest import make_event
from app.database import PosTransaction


class TestMetricsEmpty:
    def test_empty_store_returns_zero_metrics(self, client):
        resp = client.get("/stores/STORE_EMPTY_99/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["total_entries"] == 0
        assert body["total_exits"] == 0
        assert body["purchases"] == 0
        assert body["abandonment_rate"] == 0.0
        assert isinstance(body["avg_dwell_per_zone"], dict)


class TestMetricsVisitors:
    def test_unique_visitors_counted_correctly(self, client):
        events = [make_event(store_id="STORE_M1", visitor_id=f"VIS_{i}") for i in range(5)]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_M1/metrics")
        assert resp.json()["unique_visitors"] == 5

    def test_staff_excluded_from_unique_visitors(self, client):
        store = "STORE_M_STAFF"
        events = [
            make_event(store_id=store, visitor_id="VIS_C1"),
            make_event(store_id=store, visitor_id="VIS_C2"),
            make_event(store_id=store, visitor_id="VIS_STAFF1", is_staff=True),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/metrics")
        assert resp.json()["unique_visitors"] == 2

    def test_reentry_does_not_double_count_unique_visitors(self, client):
        store = "STORE_M_REENTRY"
        vid = "VIS_REENTRY_01"
        events = [
            make_event(store_id=store, visitor_id=vid, event_type="ENTRY"),
            make_event(store_id=store, visitor_id=vid, event_type="EXIT"),
            make_event(store_id=store, visitor_id=vid, event_type="REENTRY"),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/metrics")
        assert resp.json()["unique_visitors"] == 1


class TestMetricsConversion:
    def test_zero_purchase_conversion_rate_is_zero(self, client):
        store = "STORE_M_CONV0"
        events = [make_event(store_id=store, visitor_id="VIS_A")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/metrics")
        assert resp.json()["conversion_rate"] == 0.0

    def test_purchases_from_pos_reflected(self, client, db):
        store = "STORE_M_POS"
        events = [make_event(store_id=store, visitor_id=f"VIS_{i}") for i in range(4)]
        client.post("/events/ingest", json={"events": events})

        # Add POS transactions
        db.add(PosTransaction(
            transaction_id=str(uuid.uuid4()),
            store_id=store,
            timestamp=datetime.utcnow(),
            basket_value_inr=250.0,
        ))
        db.add(PosTransaction(
            transaction_id=str(uuid.uuid4()),
            store_id=store,
            timestamp=datetime.utcnow(),
            basket_value_inr=150.0,
        ))
        db.commit()

        resp = client.get(f"/stores/{store}/metrics")
        body = resp.json()
        assert body["purchases"] == 2
        assert body["conversion_rate"] > 0.0


class TestMetricsAbandon:
    def test_abandonment_rate_computed(self, client):
        store = "STORE_M_ABANDON"
        # 2 visitors join billing, 1 abandons
        events = [
            make_event(store_id=store, visitor_id="VIS_B1", event_type="BILLING_QUEUE_JOIN",
                       zone_id="BILLING", queue_depth=3),
            make_event(store_id=store, visitor_id="VIS_B2", event_type="BILLING_QUEUE_JOIN",
                       zone_id="BILLING", queue_depth=3),
            make_event(store_id=store, visitor_id="VIS_B2", event_type="BILLING_QUEUE_ABANDON",
                       zone_id="BILLING"),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/metrics")
        body = resp.json()
        assert body["abandonment_rate"] > 0.0
        assert body["abandonment_rate"] <= 100.0


class TestMetricsDwell:
    def test_avg_dwell_per_zone_populated(self, client):
        store = "STORE_M_DWELL"
        events = [
            make_event(store_id=store, visitor_id="VIS_D1", event_type="ZONE_DWELL",
                       zone_id="SKINCARE", dwell_ms=10000),
            make_event(store_id=store, visitor_id="VIS_D2", event_type="ZONE_DWELL",
                       zone_id="SKINCARE", dwell_ms=20000),
            make_event(store_id=store, visitor_id="VIS_D3", event_type="ZONE_DWELL",
                       zone_id="HAIRCARE", dwell_ms=5000),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/metrics")
        body = resp.json()
        assert "SKINCARE" in body["avg_dwell_per_zone"]
        assert body["avg_dwell_per_zone"]["SKINCARE"] == 15000.0
        assert "HAIRCARE" in body["avg_dwell_per_zone"]

    def test_avg_dwell_empty_is_empty_dict(self, client):
        store = "STORE_M_NODWELL"
        events = [make_event(store_id=store)]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/metrics")
        assert resp.json()["avg_dwell_per_zone"] == {}
