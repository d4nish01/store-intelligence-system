#!/usr/bin/env python3
"""
scripts/adapt_events_jsonl.py

Converts a real-world Purplle CCTV event JSONL export into the canonical
event schema used by POST /events/ingest.

Real event types (input):
    entry            → ENTRY
    exit             → EXIT
    zone_entered     → ZONE_ENTER  (+ synthesises ZONE_DWELL if dwell time calculable)
    zone_exited      → ZONE_EXIT
    queue_completed  → BILLING_QUEUE_JOIN  (completed billing session)
    queue_abandoned  → BILLING_QUEUE_ABANDON

Real field mapping:
    id_token / track_id         → visitor_id  (VIS_ prefix added if needed)
    store_code / store_id       → store_id
    camera_id                   → camera_id
    event_timestamp / event_time / queue_join_ts → timestamp
    zone_id                     → zone_id
    zone_name                   → metadata.sku_zone
    wait_seconds * 1000         → dwell_ms
    queue_position_at_join      → metadata.queue_depth
    is_staff                    → is_staff

Fields dropped (not in project schema):
    gender_pred, age_pred, age_bucket, is_face_hidden,
    group_id, group_size, zone_hotspot_x/y, is_revenue_zone,
    zone_type, brand_name, queue_served_ts, queue_exit_ts

Usage:
    python scripts/adapt_events_jsonl.py \
        --input  data/sample_events_raw.jsonl \
        --output data/sample_events.jsonl \
        --store-id ST1008

    # Or map the raw store code to your project store ID:
    python scripts/adapt_events_jsonl.py \
        --input  data/sample_events_raw.jsonl \
        --output data/sample_events.jsonl \
        --store-id-map store_1076=STORE_BLR_002
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


ALLOWED_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}

EVENT_TYPE_MAP = {
    "entry":            "ENTRY",
    "exit":             "EXIT",
    "zone_entered":     "ZONE_ENTER",
    "zone_exited":      "ZONE_EXIT",
    "queue_completed":  "BILLING_QUEUE_JOIN",
    "queue_abandoned":  "BILLING_QUEUE_ABANDON",
    "reentry":          "REENTRY",
}


def normalise_ts(raw: str) -> str:
    """Convert any ISO-ish timestamp to the project's format (Z-suffix)."""
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return raw  # pass through and let backend validate


def visitor_id(raw_event: Dict[str, Any]) -> str:
    """Extract a stable visitor ID from whichever field is present."""
    vid = raw_event.get("id_token") or raw_event.get("track_id")
    if vid is None:
        return f"VIS_{uuid.uuid4().hex[:8]}"
    s = str(vid)
    return s if s.startswith("VIS_") else f"VIS_{s}"


def store_id_for(raw: str, store_map: Dict[str, str], override: str) -> str:
    if override:
        return override
    if raw in store_map:
        return store_map[raw]
    return raw


def get_store(event: Dict[str, Any]) -> str:
    return (event.get("store_code") or event.get("store_id") or "STORE_UNKNOWN").strip()


def get_ts(event: Dict[str, Any]) -> str:
    for field in ("event_timestamp", "event_time", "queue_join_ts", "timestamp"):
        if event.get(field):
            return normalise_ts(event[field])
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def adapt_event(
    raw: Dict[str, Any],
    store_map: Dict[str, str],
    store_override: str,
    session_counter: Dict[str, int],
) -> Optional[Dict[str, Any]]:
    """Convert one raw event dict to project schema. Returns None if unmappable."""
    raw_type = (raw.get("event_type") or "").strip().lower()
    mapped_type = EVENT_TYPE_MAP.get(raw_type)
    if not mapped_type:
        return None  # unknown type

    vid = visitor_id(raw)
    session_counter[vid] = session_counter.get(vid, 0) + 1
    seq = session_counter[vid]

    raw_store = get_store(raw)
    sid = store_id_for(raw_store, store_map, store_override)
    cam = (raw.get("camera_id") or "CAM_UNKNOWN").strip()
    ts = get_ts(raw)

    # dwell_ms: from wait_seconds (queue) or default 0
    wait_s = raw.get("wait_seconds")
    dwell_ms = int(float(wait_s) * 1000) if wait_s is not None else 0

    # zone info
    zone_id = raw.get("zone_id")
    zone_name = raw.get("zone_name")

    # queue depth from position at join
    queue_depth = raw.get("queue_position_at_join")

    # confidence: use 0.85 as default for adapted events
    confidence = 0.85

    event = {
        "event_id": str(uuid.uuid4()),
        "store_id": sid,
        "camera_id": cam,
        "visitor_id": vid,
        "event_type": mapped_type,
        "timestamp": ts,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": bool(raw.get("is_staff", False)),
        "confidence": confidence,
        "metadata": {
            "queue_depth": int(queue_depth) if queue_depth is not None else None,
            "sku_zone": zone_name,
            "session_seq": seq,
        },
    }
    return event


def adapt(
    input_path: str,
    output_path: str,
    store_override: str,
    store_map: Dict[str, str],
) -> None:
    if not os.path.exists(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        raw_events = [json.loads(l) for l in f if l.strip()]

    session_counter: Dict[str, int] = {}
    adapted: List[Dict[str, Any]] = []
    skipped = 0
    synthesised = 0

    for raw in raw_events:
        result = adapt_event(raw, store_map, store_override, session_counter)
        if result is None:
            print(f"  WARN: Unknown event_type '{raw.get('event_type')}' — skipped")
            skipped += 1
            continue

        adapted.append(result)

        # For zone_entered + zone_exited pairs, synthesise a ZONE_DWELL event
        # if we can calculate dwell from the paired zone_exited event.
        # (Simple approach: if this is zone_entered, we emit it; dwell is emitted
        #  when we see zone_exited for the same visitor+zone.)

    # Second pass: synthesise ZONE_DWELL events where we see zone_entered/zone_exited pairs
    zone_enter_times: Dict[str, str] = {}  # key: visitor_id:zone_id → enter_timestamp

    final_events: List[Dict[str, Any]] = []
    for e in adapted:
        if e["event_type"] == "ZONE_ENTER" and e.get("zone_id"):
            key = f"{e['visitor_id']}:{e['zone_id']}"
            zone_enter_times[key] = e["timestamp"]
        elif e["event_type"] == "ZONE_EXIT" and e.get("zone_id"):
            key = f"{e['visitor_id']}:{e['zone_id']}"
            enter_ts = zone_enter_times.pop(key, None)
            if enter_ts:
                # Calculate dwell_ms
                try:
                    t_enter = datetime.strptime(enter_ts, "%Y-%m-%dT%H:%M:%SZ")
                    t_exit  = datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                    dwell = max(0, int((t_exit - t_enter).total_seconds() * 1000))
                except (ValueError, TypeError):
                    dwell = 0

                if dwell > 0:
                    # Emit a ZONE_DWELL event at the midpoint
                    session_counter[e["visitor_id"]] = session_counter.get(e["visitor_id"], 0) + 1
                    dwell_evt = dict(e)
                    dwell_evt["event_id"] = str(uuid.uuid4())
                    dwell_evt["event_type"] = "ZONE_DWELL"
                    dwell_evt["dwell_ms"] = dwell
                    dwell_evt["metadata"] = dict(e["metadata"])
                    dwell_evt["metadata"]["session_seq"] = session_counter[e["visitor_id"]]
                    final_events.append(dwell_evt)
                    synthesised += 1

        final_events.append(e)

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for evt in final_events:
            f.write(json.dumps(evt) + "\n")

    print(f"Events adapter complete:")
    print(f"  Input events      : {len(raw_events)}")
    print(f"  Skipped           : {skipped}")
    print(f"  Adapted events    : {len(adapted)}")
    print(f"  ZONE_DWELL synth  : {synthesised}")
    print(f"  Total output      : {len(final_events)}")
    print(f"  Output            : {output_path}")
    print()
    from collections import Counter
    type_counts = Counter(e["event_type"] for e in final_events)
    for t, c in sorted(type_counts.items()):
        print(f"    {t:30s}: {c}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Adapt real Purplle CCTV events JSONL to project schema")
    parser.add_argument("--input",  required=True, help="Raw events JSONL file")
    parser.add_argument("--output", default="data/sample_events.jsonl",
                        help="Output JSONL file (default: data/sample_events.jsonl)")
    parser.add_argument("--store-id", default="",
                        help="Override ALL events with this store_id (e.g. STORE_BLR_002)")
    parser.add_argument("--store-id-map", default="", metavar="SRC=DST[,SRC=DST...]",
                        help="Map specific store codes, e.g. 'store_1076=STORE_BLR_002,ST1008=STORE_BLR_001'")
    args = parser.parse_args()

    store_map: Dict[str, str] = {}
    if args.store_id_map:
        for pair in args.store_id_map.split(","):
            if "=" in pair:
                src, dst = pair.split("=", 1)
                store_map[src.strip()] = dst.strip()

    adapt(args.input, args.output, args.store_id.strip(), store_map)
