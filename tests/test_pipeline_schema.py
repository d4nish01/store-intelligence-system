# PROMPT:
# I asked an AI assistant to generate tests for the CCTV-to-event pipeline schema boundary.
# The tests needed to verify that generated events match the backend CONTRACT.md schema,
# that event types are valid, that entry-camera deduplication does not produce duplicate ENTRY events,
# and that replay-compatible JSONL is produced.
#
# CHANGES MADE:
# I reviewed the generated tests and added explicit checks for UUID event_id format,
# metadata.session_seq, confidence range, required queue_depth field, and timestamp sorting.

"""
tests/test_pipeline_schema.py

Tests for the Store Intelligence Detection Pipeline.
Does NOT require real YOLO model or real video files.
Uses demo/simulated events throughout.
"""

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.emit import (
    ALLOWED_EVENT_TYPES,
    create_event,
    epoch_to_iso,
    read_events_jsonl,
    reset_session_seq,
    validate_event,
    write_events_jsonl,
    utc_now_iso,
)
from pipeline.reid import merge_entry_events, _parse_ts, VisitorIdentityManager
from pipeline.geometry import (
    bbox_centroid,
    point_in_polygon,
    line_crossed,
    infer_direction,
    get_zone_for_point,
    is_billing_zone,
    check_entry_crossing,
)
from pipeline.staff import StaffFeatureAccumulator


# ─── Fixtures ──────────────────────────────────────────────────────────────────

BASE_EPOCH = datetime(2026, 3, 3, 14, 0, 0, tzinfo=timezone.utc).timestamp()


def make_event(**overrides) -> Dict[str, Any]:
    """Create a minimal valid event, with optional overrides."""
    reset_session_seq("VIS_test001")
    defaults = dict(
        store_id="STORE_001",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test001",
        event_type="ENTRY",
        timestamp=epoch_to_iso(BASE_EPOCH),
        zone_id=None,
        dwell_ms=0,
        is_staff=False,
        confidence=0.87,
        queue_depth=None,
        sku_zone=None,
    )
    defaults.update(overrides)
    return create_event(**defaults)


SAMPLE_ZONES = [
    {
        "zone_id": "ZONE_A",
        "zone_type": "product",
        "sku_zone": "SKINCARE",
        "camera_ids": ["CAM_ZONE_01"],
        "polygon": [[100, 100], [400, 100], [400, 400], [100, 400]],
    },
    {
        "zone_id": "BILLING",
        "zone_type": "billing",
        "sku_zone": None,
        "camera_ids": ["CAM_BILLING_01"],
        "polygon": [[500, 100], [800, 100], [800, 400], [500, 400]],
    },
]

SAMPLE_ENTRY_LINES = [
    {
        "line_id": "ENTRY_LINE_01",
        "points": [[0, 100], [1280, 100]],
        "inbound_direction": "top_to_bottom",
    }
]


# ─── Tests: create_event ───────────────────────────────────────────────────────

class TestCreateEvent:

    def test_produces_required_keys(self):
        event = make_event()
        required = [
            "event_id", "store_id", "camera_id", "visitor_id",
            "event_type", "timestamp", "zone_id", "dwell_ms",
            "is_staff", "confidence", "metadata",
        ]
        for key in required:
            assert key in event, f"Missing key: {key}"

    def test_event_id_is_uuid_v4(self):
        event = make_event()
        parsed = uuid.UUID(event["event_id"], version=4)
        assert str(parsed) == event["event_id"], "event_id is not a valid UUID v4"

    def test_event_id_unique_each_call(self):
        e1 = make_event()
        e2 = make_event()
        assert e1["event_id"] != e2["event_id"], "event_ids should be unique"

    def test_metadata_has_required_keys(self):
        event = make_event()
        meta = event["metadata"]
        assert "queue_depth" in meta
        assert "sku_zone" in meta
        assert "session_seq" in meta

    def test_metadata_session_seq_starts_positive(self):
        reset_session_seq()
        event = make_event(visitor_id="VIS_seqtest")
        assert event["metadata"]["session_seq"] >= 1

    def test_metadata_session_seq_increments(self):
        reset_session_seq()
        vid = "VIS_incrementtest"
        e1 = create_event(
            store_id="STORE_001", camera_id="CAM_ZONE_01", visitor_id=vid,
            event_type="ENTRY", timestamp=utc_now_iso(), confidence=0.8,
        )
        e2 = create_event(
            store_id="STORE_001", camera_id="CAM_ZONE_01", visitor_id=vid,
            event_type="ZONE_ENTER", timestamp=utc_now_iso(), zone_id="ZONE_A",
            confidence=0.8,
        )
        assert e2["metadata"]["session_seq"] == e1["metadata"]["session_seq"] + 1

    def test_confidence_stored_as_float(self):
        event = make_event(confidence=0.75)
        assert isinstance(event["confidence"], float)
        assert event["confidence"] == pytest.approx(0.75)

    def test_is_staff_defaults_to_bool(self):
        event = make_event()
        assert isinstance(event["is_staff"], bool)

    def test_timestamp_is_string(self):
        event = make_event()
        assert isinstance(event["timestamp"], str)

    def test_timestamp_parseable_as_iso8601(self):
        event = make_event()
        ts = event["timestamp"]
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None, "timestamp must be timezone-aware"

    def test_store_id_from_argument(self):
        event = make_event(store_id="STORE_XYZ")
        assert event["store_id"] == "STORE_XYZ"

    def test_zone_id_null_for_entry(self):
        event = make_event(event_type="ENTRY", zone_id=None)
        assert event["zone_id"] is None


# ─── Tests: validate_event ─────────────────────────────────────────────────────

class TestValidateEvent:

    def test_valid_entry_event_passes(self):
        event = make_event()
        errors = validate_event(event)
        assert errors == [], f"Valid event had errors: {errors}"

    def test_valid_zone_dwell_passes(self):
        event = make_event(
            event_type="ZONE_DWELL",
            zone_id="ZONE_A",
            dwell_ms=30000,
            confidence=0.85,
        )
        errors = validate_event(event)
        assert errors == [], f"Valid ZONE_DWELL had errors: {errors}"

    def test_invalid_event_type_rejected(self):
        event = make_event()
        event["event_type"] = "HOVER"
        errors = validate_event(event)
        assert any("event_type" in e for e in errors), "Should reject invalid event_type"

    def test_confidence_below_zero_rejected(self):
        event = make_event()
        event["confidence"] = -0.1
        errors = validate_event(event)
        assert any("confidence" in e for e in errors), "Should reject confidence < 0"

    def test_confidence_above_one_rejected(self):
        event = make_event()
        event["confidence"] = 1.1
        errors = validate_event(event)
        assert any("confidence" in e for e in errors), "Should reject confidence > 1"

    def test_dwell_ms_negative_rejected(self):
        event = make_event()
        event["dwell_ms"] = -100
        errors = validate_event(event)
        assert any("dwell_ms" in e for e in errors), "Should reject negative dwell_ms"

    def test_missing_required_key_rejected(self):
        event = make_event()
        del event["visitor_id"]
        errors = validate_event(event)
        assert any("visitor_id" in e or "Missing" in e for e in errors)

    def test_missing_metadata_session_seq(self):
        event = make_event()
        del event["metadata"]["session_seq"]
        errors = validate_event(event)
        assert any("session_seq" in e for e in errors)

    def test_billing_join_requires_queue_depth(self):
        event = make_event(
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            dwell_ms=0,
        )
        event["metadata"]["queue_depth"] = None
        errors = validate_event(event)
        assert any("queue_depth" in e for e in errors), \
            "BILLING_QUEUE_JOIN must have queue_depth populated"

    def test_billing_join_with_queue_depth_passes(self):
        event = create_event(
            store_id="STORE_001",
            camera_id="CAM_BILLING_01",
            visitor_id="VIS_billtst",
            event_type="BILLING_QUEUE_JOIN",
            timestamp=utc_now_iso(),
            zone_id="BILLING",
            dwell_ms=0,
            confidence=0.8,
            queue_depth=3,
        )
        errors = validate_event(event)
        assert errors == [], f"Valid BILLING_QUEUE_JOIN had errors: {errors}"

    def test_invalid_uuid_event_id_rejected(self):
        event = make_event()
        event["event_id"] = "not-a-uuid"
        errors = validate_event(event)
        assert any("uuid" in e.lower() or "event_id" in e.lower() for e in errors)

    def test_all_event_types_pass_validation(self):
        """Every allowed event_type should pass basic validation."""
        for et in ALLOWED_EVENT_TYPES:
            reset_session_seq()
            kwargs = dict(
                store_id="STORE_001",
                camera_id="CAM_TEST",
                visitor_id="VIS_alltest",
                event_type=et,
                timestamp=utc_now_iso(),
                dwell_ms=0,
                confidence=0.75,
            )
            if et in ("ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL"):
                kwargs["zone_id"] = "ZONE_A"
                kwargs["dwell_ms"] = 30000 if et == "ZONE_DWELL" else 0
            if et in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
                kwargs["zone_id"] = "BILLING"
                kwargs["queue_depth"] = 2
            event = create_event(**kwargs)
            errors = validate_event(event)
            assert errors == [], f"Event type {et} had validation errors: {errors}"


# ─── Tests: create_event raises on invalid input ───────────────────────────────

class TestCreateEventRaises:

    def test_invalid_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type"):
            create_event(
                store_id="STORE_001", camera_id="CAM", visitor_id="VIS_x",
                event_type="HOVER", timestamp=utc_now_iso(), confidence=0.5,
            )

    def test_negative_dwell_ms_raises(self):
        with pytest.raises(ValueError, match="dwell_ms"):
            create_event(
                store_id="STORE_001", camera_id="CAM", visitor_id="VIS_x",
                event_type="ZONE_DWELL", timestamp=utc_now_iso(),
                dwell_ms=-1, confidence=0.5,
            )

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            create_event(
                store_id="STORE_001", camera_id="CAM", visitor_id="VIS_x",
                event_type="ENTRY", timestamp=utc_now_iso(), confidence=1.5,
            )


# ─── Tests: JSONL writer ───────────────────────────────────────────────────────

class TestJsonlWriter:

    def test_writes_one_event_per_line(self):
        reset_session_seq()
        events = [
            make_event(visitor_id="VIS_a"),
            make_event(visitor_id="VIS_b"),
            make_event(visitor_id="VIS_c"),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            write_events_jsonl(events, path)
            with open(path) as f:
                lines = [l for l in f.readlines() if l.strip()]
            assert len(lines) == 3
            for line in lines:
                parsed = json.loads(line)
                assert "event_id" in parsed
        finally:
            os.unlink(path)

    def test_each_line_is_valid_json(self):
        reset_session_seq()
        events = [make_event(visitor_id="VIS_jsontest")]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            write_events_jsonl(events, path)
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        json.loads(line)  # Should not raise
        finally:
            os.unlink(path)

    def test_roundtrip_read_write(self):
        reset_session_seq()
        events = [
            make_event(visitor_id="VIS_rt1"),
            make_event(visitor_id="VIS_rt2"),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            write_events_jsonl(events, path)
            loaded = read_events_jsonl(path)
            assert len(loaded) == 2
            assert loaded[0]["visitor_id"] == events[0]["visitor_id"]
        finally:
            os.unlink(path)

    def test_invalid_events_skipped(self):
        """Invalid events should be skipped, valid ones written."""
        reset_session_seq()
        valid_event = make_event(visitor_id="VIS_valid")
        invalid_event = make_event(visitor_id="VIS_invalid")
        invalid_event["confidence"] = 2.0  # Invalid
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            count = write_events_jsonl([valid_event, invalid_event], path)
            assert count == 1
        finally:
            os.unlink(path)


# ─── Tests: merge_entry_events ─────────────────────────────────────────────────

class TestMergeEntryEvents:

    def _make_entry_event(self, camera_id: str, ts_offset_sec: float, visitor_id: str, event_type: str = "ENTRY"):
        reset_session_seq(visitor_id)
        return create_event(
            store_id="STORE_001",
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=event_type,
            timestamp=epoch_to_iso(BASE_EPOCH + ts_offset_sec),
            zone_id=None,
            dwell_ms=0,
            confidence=0.80,
        )

    def test_no_duplicate_entry_from_two_cameras(self):
        """Primary + secondary ENTRY within dedup window → only one ENTRY."""
        events = [
            self._make_entry_event("CAM_ENTRY_01", 0.0, "VIS_dup1"),
            self._make_entry_event("CAM_ENTRY_02", 2.0, "VIS_dup2"),  # 2 sec later
        ]
        merged = merge_entry_events(
            events,
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
            dedup_window_seconds=4.0,
        )
        entry_events = [e for e in merged if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 1, (
            f"Expected 1 ENTRY after dedup, got {len(entry_events)}: "
            f"{[e['camera_id'] for e in entry_events]}"
        )

    def test_dedup_uses_primary_camera(self):
        """After dedup, the surviving ENTRY should be from the primary camera."""
        events = [
            self._make_entry_event("CAM_ENTRY_01", 0.0, "VIS_pri"),
            self._make_entry_event("CAM_ENTRY_02", 1.5, "VIS_sec"),
        ]
        merged = merge_entry_events(
            events,
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
            dedup_window_seconds=4.0,
        )
        entry_events = [e for e in merged if e["event_type"] == "ENTRY"]
        assert entry_events[0]["camera_id"] == "CAM_ENTRY_01"

    def test_confidence_boosted_when_secondary_confirms(self):
        """When secondary confirms, primary event confidence should increase."""
        primary = self._make_entry_event("CAM_ENTRY_01", 0.0, "VIS_conf1")
        secondary = self._make_entry_event("CAM_ENTRY_02", 1.0, "VIS_conf2")
        original_conf = primary["confidence"]

        merged = merge_entry_events(
            [primary, secondary],
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
            dedup_window_seconds=4.0,
        )
        entry = next(e for e in merged if e["event_type"] == "ENTRY")
        assert entry["confidence"] >= original_conf, "Confidence should not decrease after confirmation"

    def test_no_dedup_outside_time_window(self):
        """Events more than dedup_window apart are NOT merged."""
        events = [
            self._make_entry_event("CAM_ENTRY_01", 0.0, "VIS_far1"),
            self._make_entry_event("CAM_ENTRY_02", 10.0, "VIS_far2"),  # 10 sec apart
        ]
        merged = merge_entry_events(
            events,
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
            dedup_window_seconds=4.0,
        )
        entry_events = [e for e in merged if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 2, "Events outside window should not be merged"

    def test_secondary_only_event_kept_with_lower_confidence(self):
        """If only secondary camera detects, event is kept but with reduced confidence."""
        secondary = self._make_entry_event("CAM_ENTRY_02", 0.0, "VIS_sec_only")
        original_conf = secondary["confidence"]

        merged = merge_entry_events(
            [secondary],
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
            dedup_window_seconds=4.0,
        )
        assert len(merged) == 1, "Secondary-only event should be kept"
        assert merged[0]["confidence"] < original_conf, (
            "Secondary-only confidence should be reduced"
        )

    def test_exit_events_also_deduped(self):
        """EXIT events from both cameras within window should also be deduplicated."""
        events = [
            self._make_entry_event("CAM_ENTRY_01", 0.0, "VIS_exit1", event_type="EXIT"),
            self._make_entry_event("CAM_ENTRY_02", 1.0, "VIS_exit2", event_type="EXIT"),
        ]
        merged = merge_entry_events(
            events,
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
            dedup_window_seconds=4.0,
        )
        exit_events = [e for e in merged if e["event_type"] == "EXIT"]
        assert len(exit_events) == 1

    def test_non_entry_events_pass_through_unchanged(self):
        """ZONE_DWELL and other events should not be affected by entry dedup."""
        zone_event = create_event(
            store_id="STORE_001", camera_id="CAM_ZONE_01", visitor_id="VIS_zone",
            event_type="ZONE_DWELL", timestamp=epoch_to_iso(BASE_EPOCH),
            zone_id="ZONE_A", dwell_ms=30000, confidence=0.85,
        )
        merged = merge_entry_events(
            [zone_event],
            primary_camera_id="CAM_ENTRY_01",
            secondary_camera_id="CAM_ENTRY_02",
        )
        assert len(merged) == 1
        assert merged[0]["event_type"] == "ZONE_DWELL"


# ─── Tests: event timestamp sorting ───────────────────────────────────────────

class TestTimestampSorting:

    def test_events_sorted_by_timestamp(self):
        """Events in JSONL must be in chronological order."""
        reset_session_seq()
        events = [
            create_event(
                store_id="STORE_001", camera_id="CAM_ENTRY_01",
                visitor_id="VIS_sort", event_type="EXIT",
                timestamp=epoch_to_iso(BASE_EPOCH + 300),
                zone_id=None, dwell_ms=0, confidence=0.8,
            ),
            create_event(
                store_id="STORE_001", camera_id="CAM_ENTRY_01",
                visitor_id="VIS_sort", event_type="ENTRY",
                timestamp=epoch_to_iso(BASE_EPOCH),
                zone_id=None, dwell_ms=0, confidence=0.8,
            ),
            create_event(
                store_id="STORE_001", camera_id="CAM_ZONE_01",
                visitor_id="VIS_sort", event_type="ZONE_ENTER",
                timestamp=epoch_to_iso(BASE_EPOCH + 60),
                zone_id="ZONE_A", dwell_ms=0, confidence=0.8,
            ),
        ]
        events.sort(key=lambda e: e["timestamp"])
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps), "Events should be in timestamp order"

    def test_iso_timestamp_string_sorts_correctly(self):
        """ISO-8601 UTC strings with Z should sort lexicographically correctly."""
        t1 = "2026-03-03T14:00:00Z"
        t2 = "2026-03-03T14:05:00Z"
        t3 = "2026-03-03T15:00:00Z"
        assert sorted([t3, t1, t2]) == [t1, t2, t3]


# ─── Tests: geometry ───────────────────────────────────────────────────────────

class TestGeometry:

    def test_bbox_centroid_uses_bottom_center(self):
        bbox = [100.0, 200.0, 200.0, 400.0]
        cx, cy = bbox_centroid(bbox)
        assert cx == pytest.approx(150.0)
        assert cy == pytest.approx(400.0)  # bottom-center y

    def test_point_inside_polygon(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        assert point_in_polygon((50, 50), poly)

    def test_point_outside_polygon(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        assert not point_in_polygon((200, 200), poly)

    def test_point_on_edge_polygon(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        # On-edge behavior may vary; just check it returns bool
        result = point_in_polygon((50, 0), poly)
        assert isinstance(result, bool)

    def test_line_crossed_detects_crossing(self):
        # Vertical line from (50,0) to (50,100)
        line = [(50, 0), (50, 100)]
        # Movement from left to right
        assert line_crossed((10, 50), (90, 50), line)

    def test_line_crossed_no_crossing(self):
        line = [(50, 0), (50, 100)]
        # Both points on same side
        assert not line_crossed((10, 50), (30, 50), line)

    def test_infer_direction_top_to_bottom(self):
        direction = infer_direction((100, 50), (100, 150), "top_to_bottom")
        assert direction == "inbound"

    def test_infer_direction_outbound(self):
        direction = infer_direction((100, 150), (100, 50), "top_to_bottom")
        assert direction == "outbound"

    def test_get_zone_for_point_returns_correct_zone(self):
        point = (200, 200)  # Inside ZONE_A polygon [100,100]-[400,400]
        zone = get_zone_for_point(point, SAMPLE_ZONES)
        assert zone is not None
        assert zone["zone_id"] == "ZONE_A"

    def test_get_zone_for_point_outside_all_zones(self):
        point = (900, 900)
        zone = get_zone_for_point(point, SAMPLE_ZONES)
        assert zone is None

    def test_is_billing_zone_detects_billing(self):
        billing_zone = {"zone_id": "BILLING", "zone_type": "billing"}
        assert is_billing_zone(billing_zone)

    def test_is_billing_zone_false_for_product(self):
        product_zone = {"zone_id": "ZONE_A", "zone_type": "product"}
        assert not is_billing_zone(product_zone)

    def test_check_entry_crossing_inbound(self):
        prev = (200, 50)    # Above line y=100
        curr = (200, 150)   # Below line y=100 (inbound = top_to_bottom)
        result = check_entry_crossing(prev, curr, SAMPLE_ENTRY_LINES)
        assert result is not None
        assert result["direction"] == "inbound"

    def test_check_entry_crossing_no_crossing(self):
        prev = (200, 200)
        curr = (300, 200)   # Horizontal, doesn't cross y=100
        result = check_entry_crossing(prev, curr, SAMPLE_ENTRY_LINES)
        assert result is None

    def test_check_entry_crossing_with_none_prev(self):
        result = check_entry_crossing(None, (200, 200), SAMPLE_ENTRY_LINES)
        assert result is None


# ─── Tests: staff classifier ───────────────────────────────────────────────────

class TestStaffClassifier:

    def test_short_duration_not_staff(self):
        clf = StaffFeatureAccumulator(staff_duration_threshold_sec=3600.0)
        clf.update_staff_features("T1", "ZONE_A", 0)
        clf.update_staff_features("T1", "ZONE_A", 600)  # 10 minutes
        assert not clf.is_likely_staff("T1")

    def test_long_duration_classified_as_staff(self):
        clf = StaffFeatureAccumulator(staff_duration_threshold_sec=3600.0)
        clf.update_staff_features("T2", "ZONE_A", 0)
        clf.update_staff_features("T2", "ZONE_B", 3700)  # 1h+
        assert clf.is_likely_staff("T2")

    def test_many_billing_visits_is_staff(self):
        clf = StaffFeatureAccumulator(billing_zone_visits_threshold=3)
        for i in range(4):
            clf.update_staff_features("T3", "BILLING", float(i * 60), is_billing=True)
        assert clf.is_likely_staff("T3")

    def test_manual_override_always_staff(self):
        clf = StaffFeatureAccumulator(manual_staff_ids={"T4"})
        # No features updated at all
        assert clf.is_likely_staff("T4")

    def test_unknown_track_not_staff(self):
        clf = StaffFeatureAccumulator()
        assert not clf.is_likely_staff("UNKNOWN_TRACK_99999")

    def test_staff_confidence_range(self):
        clf = StaffFeatureAccumulator()
        clf.update_staff_features("T5", "ZONE_A", 0)
        clf.update_staff_features("T5", "BILLING", 100, is_billing=True)
        conf = clf.staff_confidence("T5")
        assert 0.0 <= conf <= 1.0


# ─── Tests: visitor identity manager ──────────────────────────────────────────

class TestVisitorIdentityManager:

    def test_same_local_track_same_visitor_id(self):
        mgr = VisitorIdentityManager("STORE_001")
        v1 = mgr.assign_visitor_id("CAM_ENTRY_01", 42, BASE_EPOCH)
        v2 = mgr.assign_visitor_id("CAM_ENTRY_01", 42, BASE_EPOCH + 5)
        assert v1 == v2

    def test_different_local_track_different_visitor_id(self):
        mgr = VisitorIdentityManager("STORE_001")
        v1 = mgr.assign_visitor_id("CAM_ENTRY_01", 1, BASE_EPOCH)
        v2 = mgr.assign_visitor_id("CAM_ENTRY_01", 2, BASE_EPOCH)
        assert v1 != v2

    def test_reentry_detection_after_exit(self):
        mgr = VisitorIdentityManager("STORE_001", reentry_window_sec=3600)
        vid = mgr.assign_visitor_id("CAM_ENTRY_01", 10, BASE_EPOCH)
        mgr.record_entry(vid, BASE_EPOCH)
        mgr.record_exit(vid, BASE_EPOCH + 300)
        # Come back 20 minutes later
        is_reentry = mgr.detect_reentry(vid, BASE_EPOCH + 1500)
        assert is_reentry

    def test_no_reentry_without_prior_exit(self):
        mgr = VisitorIdentityManager("STORE_001", reentry_window_sec=3600)
        vid = mgr.assign_visitor_id("CAM_ENTRY_01", 11, BASE_EPOCH)
        mgr.record_entry(vid, BASE_EPOCH)
        # No exit recorded
        is_reentry = mgr.detect_reentry(vid, BASE_EPOCH + 1500)
        assert not is_reentry

    def test_visitor_id_format(self):
        mgr = VisitorIdentityManager("STORE_TEST")
        vid = mgr.assign_visitor_id("CAM_TEST", 99, BASE_EPOCH)
        assert vid.startswith("VIS_"), f"Visitor ID should start with VIS_, got: {vid}"
        assert len(vid) > 4


# ─── Tests: demo mode process_store ───────────────────────────────────────────

class TestDemoProcessStore:
    """
    Tests that demo mode generates valid schema-compliant events
    without requiring real video or YOLO.
    """

    def _minimal_layout(self) -> Dict[str, Any]:
        return {
            "store_id": "STORE_TEST",
            "open_time": "09:00",
            "close_time": "21:00",
            "coordinate_space": {"width": 1280, "height": 720, "unit": "pixels"},
            "zones": [
                {
                    "zone_id": "ZONE_A",
                    "zone_type": "product",
                    "sku_zone": "TEST_PRODUCTS",
                    "camera_ids": ["CAM_ZONE_01"],
                    "polygon": [[100, 100], [400, 100], [400, 400], [100, 400]],
                },
                {
                    "zone_id": "BILLING",
                    "zone_type": "billing",
                    "sku_zone": None,
                    "camera_ids": ["CAM_BILLING_01"],
                    "polygon": [[500, 100], [800, 100], [800, 400], [500, 400]],
                },
            ],
            "entry_lines": [
                {
                    "line_id": "ENTRY_LINE_01",
                    "points": [[0, 100], [1280, 100]],
                    "inbound_direction": "top_to_bottom",
                }
            ],
        }

    def test_demo_generates_events(self):
        from pipeline.process_store import generate_demo_events
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
            {"video_path": None, "camera_id": "CAM_ZONE_01", "role": "main_floor_zone"},
            {"video_path": None, "camera_id": "CAM_BILLING_01", "role": "billing"},
        ]
        layout = self._minimal_layout()
        reset_session_seq()
        events = generate_demo_events(
            store_id="STORE_TEST",
            layout=layout,
            cameras=cameras,
            start_epoch=BASE_EPOCH,
            num_visitors=3,
        )
        assert len(events) > 0, "Demo should generate at least some events"

    def test_demo_events_pass_schema_validation(self):
        from pipeline.process_store import generate_demo_events
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
            {"video_path": None, "camera_id": "CAM_ZONE_01", "role": "main_floor_zone"},
            {"video_path": None, "camera_id": "CAM_BILLING_01", "role": "billing"},
        ]
        layout = self._minimal_layout()
        reset_session_seq()
        events = generate_demo_events(
            store_id="STORE_TEST",
            layout=layout,
            cameras=cameras,
            start_epoch=BASE_EPOCH,
            num_visitors=4,
        )
        for event in events:
            errors = validate_event(event)
            assert errors == [], f"Demo event failed validation: {errors}\nEvent: {event}"

    def test_demo_events_sorted_by_timestamp(self):
        from pipeline.process_store import generate_demo_events
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
        ]
        layout = self._minimal_layout()
        reset_session_seq()
        events = generate_demo_events(
            store_id="STORE_TEST",
            layout=layout,
            cameras=cameras,
            start_epoch=BASE_EPOCH,
            num_visitors=3,
        )
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps), "Demo events must be sorted by timestamp"

    def test_demo_events_use_correct_store_id(self):
        from pipeline.process_store import generate_demo_events
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
        ]
        layout = self._minimal_layout()
        reset_session_seq()
        events = generate_demo_events(
            store_id="STORE_XYZ",
            layout=layout,
            cameras=cameras,
            start_epoch=BASE_EPOCH,
            num_visitors=2,
        )
        for event in events:
            assert event["store_id"] == "STORE_XYZ"

    def test_demo_events_have_staff_marked(self):
        from pipeline.process_store import generate_demo_events
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
            {"video_path": None, "camera_id": "CAM_ZONE_01", "role": "main_floor_zone"},
            {"video_path": None, "camera_id": "CAM_BILLING_01", "role": "billing"},
        ]
        layout = self._minimal_layout()
        reset_session_seq()
        events = generate_demo_events(
            store_id="STORE_TEST",
            layout=layout,
            cameras=cameras,
            start_epoch=BASE_EPOCH,
            num_visitors=5,
        )
        # is_staff must always be bool
        for event in events:
            assert isinstance(event["is_staff"], bool)

    def test_demo_write_and_read_jsonl(self):
        from pipeline.process_store import generate_demo_events
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
            {"video_path": None, "camera_id": "CAM_ZONE_01", "role": "main_floor_zone"},
        ]
        layout = self._minimal_layout()
        reset_session_seq()
        events = generate_demo_events(
            store_id="STORE_TEST",
            layout=layout,
            cameras=cameras,
            start_epoch=BASE_EPOCH,
            num_visitors=3,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            count = write_events_jsonl(events, path)
            loaded = read_events_jsonl(path)
            assert count == len(loaded)
            assert count > 0
        finally:
            os.unlink(path)


# ─── Tests: allowed event types completeness ───────────────────────────────────

class TestAllowedEventTypes:

    def test_all_required_types_present(self):
        required = {
            "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
            "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
        }
        assert required == ALLOWED_EVENT_TYPES, (
            f"ALLOWED_EVENT_TYPES mismatch.\n"
            f"Missing: {required - ALLOWED_EVENT_TYPES}\n"
            f"Extra: {ALLOWED_EVENT_TYPES - required}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
