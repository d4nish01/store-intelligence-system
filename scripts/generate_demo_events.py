#!/usr/bin/env python3
"""Generate demo events in JSONL format for testing without a live CCTV pipeline."""
import sys
import os
import json
import uuid
import random
import argparse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


STORES = ["STORE_BLR_001", "STORE_BLR_002"]
CAMERAS = {
    "STORE_BLR_001": ["CAM_ENTRY_01", "CAM_ZONE_01", "CAM_ZONE_02", "CAM_BILLING_01"],
    "STORE_BLR_002": ["CAM_ENTRY_01", "CAM_ZONE_03", "CAM_BILLING_01"],
}
ZONES = ["SKINCARE", "HAIRCARE", "FRAGRANCES", "COSMETICS", "BILLING"]
SKU_ZONES = ["MOISTURISER", "SHAMPOO", "PERFUME", "FOUNDATION", "CHECKOUT"]


def make_visitor_id() -> str:
    return f"VIS_{uuid.uuid4().hex[:6]}"


def make_event(
    store_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float | None = None,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    session_seq: int = 1,
) -> dict:
    camera_id = random.choice(CAMERAS[store_id])
    if confidence is None:
        confidence = round(random.uniform(0.75, 0.99), 2)
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def generate_events(base_time: datetime, store_id: str) -> list[dict]:
    events = []
    t = base_time

    def advance(minutes: float = 1.0) -> datetime:
        nonlocal t
        t = t + timedelta(minutes=minutes)
        return t

    # ── Normal customer journey ────────────────────────────────────────────────
    for i in range(15):
        vid = make_visitor_id()
        seq = 0
        entry_t = t + timedelta(minutes=i * 3)
        events.append(make_event(store_id, vid, "ENTRY", entry_t, session_seq=seq + 1))
        seq += 1

        zone = random.choice(ZONES[:-1])
        events.append(make_event(store_id, vid, "ZONE_ENTER", entry_t + timedelta(seconds=30),
                                 zone_id=zone, session_seq=seq + 1))
        seq += 1
        dwell = random.randint(5000, 60000)
        events.append(make_event(store_id, vid, "ZONE_DWELL", entry_t + timedelta(seconds=60),
                                 zone_id=zone, dwell_ms=dwell, sku_zone=random.choice(SKU_ZONES), session_seq=seq + 1))
        seq += 1
        events.append(make_event(store_id, vid, "ZONE_EXIT", entry_t + timedelta(seconds=90),
                                 zone_id=zone, session_seq=seq + 1))
        seq += 1

        if i < 8:  # majority go to billing
            events.append(make_event(store_id, vid, "BILLING_QUEUE_JOIN",
                                     entry_t + timedelta(minutes=5),
                                     zone_id="BILLING",
                                     queue_depth=random.randint(1, 4), session_seq=seq + 1))
            seq += 1

        events.append(make_event(store_id, vid, "EXIT", entry_t + timedelta(minutes=10), session_seq=seq + 1))

    # ── Staff visitor ──────────────────────────────────────────────────────────
    staff_vid = make_visitor_id()
    events.append(make_event(store_id, staff_vid, "ENTRY", t, is_staff=True, session_seq=1))
    for zone in ZONES[:3]:
        events.append(make_event(store_id, staff_vid, "ZONE_ENTER", t + timedelta(minutes=2),
                                 zone_id=zone, is_staff=True, session_seq=2))
    events.append(make_event(store_id, staff_vid, "EXIT", t + timedelta(minutes=30), is_staff=True, session_seq=3))

    # ── Re-entry visitor ──────────────────────────────────────────────────────
    reentry_vid = make_visitor_id()
    events.append(make_event(store_id, reentry_vid, "ENTRY", t + timedelta(minutes=1), session_seq=1))
    events.append(make_event(store_id, reentry_vid, "EXIT", t + timedelta(minutes=20), session_seq=2))
    events.append(make_event(store_id, reentry_vid, "REENTRY", t + timedelta(minutes=35), session_seq=3))
    events.append(make_event(store_id, reentry_vid, "ZONE_ENTER", t + timedelta(minutes=36),
                             zone_id="SKINCARE", session_seq=4))
    events.append(make_event(store_id, reentry_vid, "EXIT", t + timedelta(minutes=50), session_seq=5))

    # ── Billing queue spike (critical) ────────────────────────────────────────
    spike_vid = make_visitor_id()
    events.append(make_event(store_id, spike_vid, "ENTRY", t + timedelta(minutes=40), session_seq=1))
    events.append(make_event(store_id, spike_vid, "BILLING_QUEUE_JOIN", t + timedelta(minutes=45),
                             zone_id="BILLING", queue_depth=9, session_seq=2))

    # ── Billing queue abandon ─────────────────────────────────────────────────
    abandon_vid = make_visitor_id()
    events.append(make_event(store_id, abandon_vid, "ENTRY", t + timedelta(minutes=50), session_seq=1))
    events.append(make_event(store_id, abandon_vid, "BILLING_QUEUE_JOIN", t + timedelta(minutes=55),
                             zone_id="BILLING", queue_depth=6, session_seq=2))
    events.append(make_event(store_id, abandon_vid, "BILLING_QUEUE_ABANDON", t + timedelta(minutes=58),
                             zone_id="BILLING", session_seq=3))
    events.append(make_event(store_id, abandon_vid, "EXIT", t + timedelta(minutes=59), session_seq=4))

    return events


def main():
    parser = argparse.ArgumentParser(description="Generate demo JSONL events")
    parser.add_argument("--output", default="data/sample_events.jsonl", help="Output file path")
    parser.add_argument("--minutes-ago", type=int, default=5,
                        help="How many minutes ago to start event timestamps (default: 5, use >10 to trigger STALE_FEED)")
    args = parser.parse_args()

    base_time = datetime.now(timezone.utc) - timedelta(minutes=args.minutes_ago)

    all_events = []
    for store_id in STORES:
        all_events.extend(generate_events(base_time, store_id))

    random.shuffle(all_events)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")

    print(f"✓ Generated {len(all_events)} events → {args.output}")
    for store_id in STORES:
        count = sum(1 for e in all_events if e["store_id"] == store_id)
        print(f"  {store_id}: {count} events")


if __name__ == "__main__":
    main()
