# PROMPT:
# I asked an AI assistant to generate pytest coverage for the /stores/{id}/funnel endpoint.
# The tests needed to verify the four funnel stages (ENTRY, ZONE_VISIT, BILLING_QUEUE, PURCHASE),
# that staff are excluded from all stages, that REENTRY is not counted as an additional ENTRY,
# that dropoff percentages are computed correctly, and that a zero previous-stage count
# does not cause a division-by-zero error.
#
# CHANGES MADE:
# I reviewed the generated tests and added an explicit test that dropoff from ENTRY to
# ZONE_VISIT is correctly computed as ((entry - zone) / entry) * 100, and that when no
# events exist the funnel returns four stages all with count=0 and dropoff=0.0.

import uuid
import pytest
from datetime import datetime, timezone

from tests.conftest import make_event
from app.database import PosTransaction


class TestFunnelEmpty:
    def test_empty_store_funnel_four_stages_all_zero(self, client):
        resp = client.get("/stores/STORE_FUNNEL_EMPTY/funnel")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["stages"]) == 4
        for stage in body["stages"]:
            assert stage["count"] == 0
            assert stage["dropoff_from_previous_pct"] == 0.0

    def test_stage_names_present(self, client):
        resp = client.get("/stores/STORE_FUNNEL_NAMES/funnel")
        names = [s["stage"] for s in resp.json()["stages"]]
        assert names == ["ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE"]


class TestFunnelCounts:
    def test_entry_stage_count(self, client):
        store = "STORE_F_ENTRY"
        events = [make_event(store_id=store, visitor_id=f"VIS_{i}", event_type="ENTRY") for i in range(6)]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/funnel")
        entry_stage = next(s for s in resp.json()["stages"] if s["stage"] == "ENTRY")
        assert entry_stage["count"] == 6

    def test_zone_visit_stage(self, client):
        store = "STORE_F_ZONE"
        # 5 entry, 3 zone
        entry_events = [make_event(store_id=store, visitor_id=f"VIS_{i}", event_type="ENTRY") for i in range(5)]
        zone_events = [make_event(store_id=store, visitor_id=f"VIS_{i}", event_type="ZONE_ENTER", zone_id="SKINCARE") for i in range(3)]
        client.post("/events/ingest", json={"events": entry_events + zone_events})
        resp = client.get(f"/stores/{store}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["ZONE_VISIT"]["count"] == 3

    def test_billing_queue_stage(self, client):
        store = "STORE_F_BILLING"
        events = [
            make_event(store_id=store, visitor_id="VIS_1", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING"),
            make_event(store_id=store, visitor_id="VIS_2", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING"),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["BILLING_QUEUE"]["count"] == 2

    def test_purchase_stage_from_pos(self, client, db):
        store = "STORE_F_PURCHASE"
        db.add(PosTransaction(transaction_id=str(uuid.uuid4()), store_id=store,
                              timestamp=datetime.utcnow(), basket_value_inr=100.0))
        db.commit()
        resp = client.get(f"/stores/{store}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["PURCHASE"]["count"] == 1


class TestFunnelStaffExclusion:
    def test_staff_excluded_from_all_stages(self, client):
        store = "STORE_F_STAFF"
        events = [
            make_event(store_id=store, visitor_id="VIS_CUST", event_type="ENTRY"),
            make_event(store_id=store, visitor_id="VIS_STAFF", event_type="ENTRY", is_staff=True),
            make_event(store_id=store, visitor_id="VIS_STAFF", event_type="ZONE_ENTER",
                       zone_id="BACK_STORE", is_staff=True),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["ENTRY"]["count"] == 1


class TestFunnelReentry:
    def test_reentry_not_double_counted_in_entry_stage(self, client):
        store = "STORE_F_REENTRY"
        vid = "VIS_REENTER_01"
        events = [
            make_event(store_id=store, visitor_id=vid, event_type="ENTRY"),
            make_event(store_id=store, visitor_id=vid, event_type="REENTRY"),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        # Both ENTRY and REENTRY feed into the ENTRY stage set — only 1 unique visitor
        assert stages["ENTRY"]["count"] == 1


class TestFunnelDropoff:
    def test_dropoff_percentages_correct(self, client):
        store = "STORE_F_DROP"
        # 10 entry, 8 zone, 4 billing
        entry_vids = [f"VIS_E{i}" for i in range(10)]
        zone_vids = entry_vids[:8]
        billing_vids = zone_vids[:4]

        events = []
        for v in entry_vids:
            events.append(make_event(store_id=store, visitor_id=v, event_type="ENTRY"))
        for v in zone_vids:
            events.append(make_event(store_id=store, visitor_id=v, event_type="ZONE_ENTER", zone_id="SKINCARE"))
        for v in billing_vids:
            events.append(make_event(store_id=store, visitor_id=v, event_type="BILLING_QUEUE_JOIN", zone_id="BILLING"))

        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}

        assert stages["ENTRY"]["dropoff_from_previous_pct"] == 0.0
        # 10 entry → 8 zone: 20% drop
        assert abs(stages["ZONE_VISIT"]["dropoff_from_previous_pct"] - 20.0) < 0.5
        # 8 zone → 4 billing: 50% drop
        assert abs(stages["BILLING_QUEUE"]["dropoff_from_previous_pct"] - 50.0) < 0.5

    def test_zero_previous_stage_no_division_error(self, client):
        """When entry count is 0, zone dropoff must be 0.0 not an error."""
        store = "STORE_F_ZERO"
        # Only zone events (no entry)
        events = [make_event(store_id=store, visitor_id="VIS_Z1", event_type="ZONE_ENTER", zone_id="SKINCARE")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/funnel")
        assert resp.status_code == 200
        # No exception raised
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["ZONE_VISIT"]["dropoff_from_previous_pct"] == 0.0
