"""
pipeline/emit.py

Responsibilities:
- Create valid event dicts matching the official event schema.
- Validate required fields and value constraints.
- Generate UUID v4 event_ids.
- Maintain session_seq per visitor_id.
- Write JSONL files.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ─── Allowed event types ────────────────────────────────────────────────────────

ALLOWED_EVENT_TYPES = {
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
}

# ─── Session sequence tracker ───────────────────────────────────────────────────

_session_seq_counters: Dict[str, int] = {}


def _next_session_seq(visitor_id: str) -> int:
    """Increment and return the next session_seq for a visitor."""
    _session_seq_counters[visitor_id] = _session_seq_counters.get(visitor_id, 0) + 1
    return _session_seq_counters[visitor_id]


def reset_session_seq(visitor_id: Optional[str] = None):
    """Reset session seq counter. Pass None to reset all."""
    global _session_seq_counters
    if visitor_id is None:
        _session_seq_counters = {}
    else:
        _session_seq_counters.pop(visitor_id, None)


# ─── Event factory ─────────────────────────────────────────────────────────────

def create_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: str,                      # ISO-8601 UTC string
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.75,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: Optional[int] = None,   # If None, auto-increment
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a validated event dict matching the official schema.

    Args:
        store_id: e.g. "STORE_001"
        camera_id: e.g. "CAM_ENTRY_01"
        visitor_id: e.g. "VIS_abc123"
        event_type: One of ALLOWED_EVENT_TYPES.
        timestamp: ISO-8601 UTC string, e.g. "2026-03-03T14:22:10Z"
        zone_id: Zone name or None (None for ENTRY/EXIT).
        dwell_ms: Dwell duration milliseconds (0 for instantaneous events).
        is_staff: True if classified as staff.
        confidence: Float 0–1.
        queue_depth: Required for BILLING_QUEUE_JOIN/ABANDON.
        sku_zone: Zone label from layout config.
        session_seq: Explicit session sequence; auto-incremented if None.
        extra_metadata: Additional metadata to merge.

    Returns:
        Event dict.

    Raises:
        ValueError: If validation fails.
    """
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"Invalid event_type '{event_type}'. Allowed: {ALLOWED_EVENT_TYPES}")

    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")

    if dwell_ms < 0:
        raise ValueError(f"dwell_ms must be >= 0, got {dwell_ms}")

    # Auto-increment session_seq if not provided
    if session_seq is None:
        session_seq = _next_session_seq(visitor_id)

    # queue_depth validation
    if event_type in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON") and queue_depth is None:
        # Allow None with a warning — backend may handle; don't hard-fail
        pass

    metadata: Dict[str, Any] = {
        "queue_depth": queue_depth,
        "sku_zone": sku_zone,
        "session_seq": session_seq,
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    event: Dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(float(confidence), 6),
        "metadata": metadata,
    }

    return event


# ─── Validation ─────────────────────────────────────────────────────────────────

def validate_event(event: Dict[str, Any]) -> List[str]:
    """
    Validate an event dict against the schema.

    Returns:
        List of error strings. Empty list means valid.
    """
    errors = []

    required_keys = [
        "event_id", "store_id", "camera_id", "visitor_id",
        "event_type", "timestamp", "zone_id", "dwell_ms",
        "is_staff", "confidence", "metadata",
    ]
    for key in required_keys:
        if key not in event:
            errors.append(f"Missing required key: {key}")

    if errors:
        return errors  # Can't validate further without required keys

    # event_id: UUID v4 format
    try:
        parsed = uuid.UUID(str(event["event_id"]), version=4)
        if str(parsed) != event["event_id"]:
            errors.append(f"event_id is not a valid UUID v4: {event['event_id']}")
    except (ValueError, AttributeError):
        errors.append(f"event_id is not a valid UUID: {event['event_id']}")

    # event_type
    if event["event_type"] not in ALLOWED_EVENT_TYPES:
        errors.append(f"Invalid event_type: {event['event_type']}")

    # confidence
    conf = event.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        errors.append(f"confidence must be float in [0,1], got {conf}")

    # dwell_ms
    dwell = event.get("dwell_ms")
    if not isinstance(dwell, (int, float)) or dwell < 0:
        errors.append(f"dwell_ms must be >= 0, got {dwell}")

    # timestamp ISO-8601 format
    ts = event.get("timestamp", "")
    try:
        if ts.endswith("Z"):
            ts_check = ts[:-1] + "+00:00"
        else:
            ts_check = ts
        datetime.fromisoformat(ts_check)
    except (ValueError, AttributeError):
        errors.append(f"timestamp is not valid ISO-8601: {ts}")

    # is_staff must be bool
    if not isinstance(event.get("is_staff"), bool):
        errors.append(f"is_staff must be bool, got {type(event.get('is_staff'))}")

    # metadata
    meta = event.get("metadata", {})
    if not isinstance(meta, dict):
        errors.append("metadata must be a dict")
    else:
        for required_meta_key in ("queue_depth", "sku_zone", "session_seq"):
            if required_meta_key not in meta:
                errors.append(f"metadata missing key: {required_meta_key}")

        session_seq = meta.get("session_seq")
        if session_seq is not None and not isinstance(session_seq, int):
            errors.append(f"metadata.session_seq must be int or null, got {type(session_seq)}")

        # Billing events must have queue_depth
        if event["event_type"] in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
            if meta.get("queue_depth") is None:
                errors.append(
                    f"metadata.queue_depth is required for {event['event_type']}"
                )

    return errors


# ─── JSONL writer ───────────────────────────────────────────────────────────────

def write_events_jsonl(events: List[Dict[str, Any]], output_path: str) -> int:
    """
    Write list of events to a JSONL file (one JSON object per line).
    Events are written in the order provided.
    Returns count of events written.
    """
    import os
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    written = 0
    invalid = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for event in events:
            errors = validate_event(event)
            if errors:
                print(f"[emit] WARNING: Invalid event skipped: {errors}")
                invalid += 1
                continue
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            written += 1

    if invalid:
        print(f"[emit] {invalid} invalid events were skipped.")
    print(f"[emit] Wrote {written} events to {output_path}")
    return written


def read_events_jsonl(input_path: str) -> List[Dict[str, Any]]:
    """Read JSONL file and return list of event dicts."""
    events = []
    with open(input_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[emit] WARNING: Invalid JSON on line {i}: {e}")
    return events


# ─── Utility: current UTC ISO timestamp ────────────────────────────────────────

def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string ending in Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_to_iso(epoch_sec: float) -> str:
    """Convert epoch seconds (float) to ISO-8601 UTC string."""
    return datetime.fromtimestamp(epoch_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
