# PROMPT:
# I asked an AI assistant to generate pytest coverage for the /stores/{id}/heatmap endpoint.
# The tests needed to verify zone visit frequency from ZONE_ENTER events, average dwell from
# ZONE_DWELL events, that normalized_score is in [0,100], that data_confidence is "LOW" when
# fewer than 20 customer sessions exist, and that an empty heatmap returns an empty zones list.
#
# CHANGES MADE:
# I reviewed the generated tests and added a check that the zone with the highest visit count
# always receives a normalized_score of exactly 100, that staff zone visits are excluded from
# the heatmap, and that a store with ≥20 sessions gets "OK" confidence.

import pytest
from datetime import datetime, timezone

from tests.conftest import make_event


class TestHeatmapEmpty:
    def test_empty_store_returns_empty_zones(self, client):
        resp = client.get("/stores/STORE_HM_EMPTY/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["zones"] == []

    def test_empty_store_has_confidence_field(self, client):
        resp = client.get("/stores/STORE_HM_EMPTY2/heatmap")
        assert "data_confidence" in resp.json()


class TestHeatmapZoneFrequency:
    def test_zone_visit_count_from_zone_enter(self, client):
        store = "STORE_HM_VISIT"
        events = [
            make_event(store_id=store, visitor_id="VIS_A", event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(store_id=store, visitor_id="VIS_B", event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(store_id=store, visitor_id="VIS_C", event_type="ZONE_ENTER", zone_id="HAIRCARE"),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert zones["SKINCARE"]["visit_count"] == 2
        assert zones["HAIRCARE"]["visit_count"] == 1

    def test_staff_excluded_from_heatmap(self, client):
        store = "STORE_HM_STAFF"
        events = [
            make_event(store_id=store, visitor_id="VIS_CUST", event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(store_id=store, visitor_id="VIS_STAFF", event_type="ZONE_ENTER",
                       zone_id="BACK_STORE", is_staff=True),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert "BACK_STORE" not in zones
        assert "SKINCARE" in zones


class TestHeatmapDwell:
    def test_avg_dwell_ms_from_zone_dwell_events(self, client):
        store = "STORE_HM_DWELL"
        events = [
            make_event(store_id=store, visitor_id="VIS_A", event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(store_id=store, visitor_id="VIS_A", event_type="ZONE_DWELL",
                       zone_id="SKINCARE", dwell_ms=10000),
            make_event(store_id=store, visitor_id="VIS_B", event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(store_id=store, visitor_id="VIS_B", event_type="ZONE_DWELL",
                       zone_id="SKINCARE", dwell_ms=20000),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert abs(zones["SKINCARE"]["avg_dwell_ms"] - 15000.0) < 1.0

    def test_zone_without_dwell_events_has_zero_avg_dwell(self, client):
        store = "STORE_HM_NODWELL"
        events = [make_event(store_id=store, visitor_id="VIS_A", event_type="ZONE_ENTER", zone_id="FRAGRANCES")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert zones["FRAGRANCES"]["avg_dwell_ms"] == 0.0


class TestHeatmapNormalized:
    def test_normalized_score_in_0_100(self, client):
        store = "STORE_HM_NORM"
        events = [
            make_event(store_id=store, visitor_id=f"VIS_{i}", event_type="ZONE_ENTER", zone_id="SKINCARE")
            for i in range(5)
        ] + [
            make_event(store_id=store, visitor_id=f"VIS_H{i}", event_type="ZONE_ENTER", zone_id="HAIRCARE")
            for i in range(2)
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        for zone in resp.json()["zones"]:
            assert 0 <= zone["normalized_score"] <= 100

    def test_max_zone_gets_score_100(self, client):
        store = "STORE_HM_MAX"
        events = [
            make_event(store_id=store, visitor_id=f"VIS_S{i}", event_type="ZONE_ENTER", zone_id="SKINCARE")
            for i in range(10)
        ] + [
            make_event(store_id=store, visitor_id=f"VIS_H{i}", event_type="ZONE_ENTER", zone_id="HAIRCARE")
            for i in range(3)
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert zones["SKINCARE"]["normalized_score"] == 100


class TestHeatmapConfidence:
    def test_low_confidence_when_fewer_than_20_sessions(self, client):
        store = "STORE_HM_LOW"
        events = [
            make_event(store_id=store, visitor_id=f"VIS_{i}", event_type="ZONE_ENTER", zone_id="SKINCARE")
            for i in range(5)
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        assert resp.json()["data_confidence"] == "LOW"

    def test_ok_confidence_when_20_or_more_sessions(self, client):
        store = "STORE_HM_OK"
        # 20 different visitors to create 20 sessions
        events = [
            make_event(store_id=store, visitor_id=f"VIS_{i}", event_type="ZONE_ENTER", zone_id="SKINCARE")
            for i in range(20)
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/heatmap")
        assert resp.json()["data_confidence"] == "OK"
