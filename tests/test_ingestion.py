# PROMPT:
# I asked an AI assistant to generate pytest coverage for the event ingestion endpoint.
# The tests needed to verify batch validation, deduplication, partial rejection when some
# events are malformed, enforcement of the 500-event batch limit, structured error
# responses without raw tracebacks, and database persistence of accepted events.
#
# CHANGES MADE:
# I reviewed the generated tests and added assertions for idempotency (same payload
# ingested twice must yield accepted=N first time, duplicates=N second time), for
# ensuring that only malformed events are rejected while valid siblings are accepted,
# and for verifying that event_id is stored exactly once in the DB.

import uuid
import pytest
from datetime import datetime, timezone

from tests.conftest import make_event
from app.database import EventModel


class TestIngestValid:
    def test_valid_batch_accepted(self, client):
        events = [make_event() for _ in range(5)]
        resp = client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 5
        assert body["duplicates"] == 0
        assert body["rejected"] == 0
        assert body["errors"] == []

    def test_event_stored_in_database(self, client, db):
        evt = make_event()
        client.post("/events/ingest", json={"events": [evt]})
        row = db.get(EventModel, evt["event_id"])
        assert row is not None
        assert row.store_id == evt["store_id"]
        assert row.visitor_id == evt["visitor_id"]
        assert row.event_type == evt["event_type"]

    def test_trace_id_in_response_header(self, client):
        events = [make_event()]
        resp = client.post("/events/ingest", json={"events": events})
        assert "x-trace-id" in resp.headers


class TestIngestDuplication:
    def test_duplicate_event_id_idempotent(self, client):
        events = [make_event()]
        # First ingest
        r1 = client.post("/events/ingest", json={"events": events})
        assert r1.json()["accepted"] == 1
        assert r1.json()["duplicates"] == 0

        # Second ingest with same event_id
        r2 = client.post("/events/ingest", json={"events": events})
        assert r2.json()["accepted"] == 0
        assert r2.json()["duplicates"] == 1

    def test_duplicate_does_not_double_count_in_db(self, client, db):
        evt = make_event()
        client.post("/events/ingest", json={"events": [evt]})
        client.post("/events/ingest", json={"events": [evt]})
        rows = db.query(EventModel).filter(EventModel.event_id == evt["event_id"]).all()
        assert len(rows) == 1


class TestIngestRejection:
    def test_malformed_event_partial_rejection(self, client):
        valid = make_event()
        invalid = {**make_event(), "confidence": 5.0}  # confidence > 1.0
        resp = client.post("/events/ingest", json={"events": [valid, invalid]})
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1
        assert len(body["errors"]) == 1

    def test_malformed_event_error_has_index(self, client):
        bad = {**make_event(), "event_type": "INVALID_TYPE"}
        resp = client.post("/events/ingest", json={"events": [bad]})
        body = resp.json()
        assert body["rejected"] == 1
        err = body["errors"][0]
        assert "index" in err
        assert err["index"] == 0

    def test_bad_uuid_rejected_with_message(self, client):
        bad = {**make_event(), "event_id": "not-a-uuid"}
        resp = client.post("/events/ingest", json={"events": [bad]})
        body = resp.json()
        assert body["rejected"] == 1
        assert body["errors"][0]["message"]

    def test_negative_dwell_ms_rejected(self, client):
        bad = {**make_event(), "dwell_ms": -100}
        resp = client.post("/events/ingest", json={"events": [bad]})
        assert resp.json()["rejected"] == 1

    def test_batch_over_500_rejected(self, client):
        events = [make_event() for _ in range(501)]
        resp = client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 400

    def test_structured_error_response_no_traceback(self, client):
        bad = {**make_event(), "confidence": -1.0}
        resp = client.post("/events/ingest", json={"events": [bad]})
        body = resp.json()
        # Should not contain Python traceback markers
        assert "Traceback" not in str(body)
        assert "rejected" in body


class TestIngestAllEventTypes:
    @pytest.mark.parametrize("event_type", [
        "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
        "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
    ])
    def test_all_valid_event_types_accepted(self, client, event_type):
        evt = make_event(event_type=event_type, zone_id="SKINCARE")
        resp = client.post("/events/ingest", json={"events": [evt]})
        assert resp.json()["accepted"] == 1, f"Expected acceptance for {event_type}"
